"""Accès SQLite — vérité applicative (§5). WAL : lectures concurrentes non
bloquantes pendant que le worker écrit. Un seul écrivain (le worker) ; la web
app LIT, sauf la table dédiée web_actions (acquittement, lu/non-lu).

Multi-actif : les tables métier portent une colonne `symbol`. En P1 seules les
tables du socle sont alimentées (heartbeat, events, health, config).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import CONFIG


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    beat_utc TEXT NOT NULL, cycle INTEGER NOT NULL,
    source TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL, kind TEXT NOT NULL, level TEXT NOT NULL,
    message TEXT NOT NULL, meta TEXT, read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts_utc);
CREATE TABLE IF NOT EXISTS health (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
    cycle_kind TEXT NOT NULL, duration_s REAL, cpu_s REAL,
    peak_mem_mb REAL, clock_skew_s REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS config_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS web_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
    action TEXT NOT NULL, target TEXT
);
-- Tables métier (forme figée, remplies en P2+) — clef par ACTIF -------------
CREATE TABLE IF NOT EXISTS probas (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, direction TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, opened_utc TEXT NOT NULL,
    closed_utc TEXT, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, payload TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Optional[Path] = None, read_only: bool = False) -> None:
        self.path = Path(path) if path else CONFIG.db_path
        self.read_only = read_only
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if read_only:
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                        timeout=30, check_same_thread=False)
        else:
            self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.cursor()
        if read_only:
            cur.execute("PRAGMA busy_timeout=30000;")
        else:
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA busy_timeout=30000;")
        cur.close()
        if not read_only:
            with self.conn:
                self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----- Heartbeat -------------------------------------------------------
    def write_heartbeat(self, cycle: int, source: str, status: str = "ok") -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO heartbeat (id, beat_utc, cycle, source, status)
                   VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                   beat_utc=excluded.beat_utc, cycle=excluded.cycle,
                   source=excluded.source, status=excluded.status""",
                (utc_now_iso(), cycle, source, status))

    def get_heartbeat(self) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM heartbeat WHERE id = 1").fetchone()
        return dict(row) if row else None

    def heartbeat_age_seconds(self) -> Optional[float]:
        hb = self.get_heartbeat()
        if not hb:
            return None
        beat = datetime.fromisoformat(hb["beat_utc"])
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - beat).total_seconds()

    # ----- Cloche / événements (§6) ---------------------------------------
    def add_event(self, kind: str, level: str, message: str,
                  meta: Optional[dict] = None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO events (ts_utc, kind, level, message, meta) VALUES (?,?,?,?,?)",
                (utc_now_iso(), kind, level, message, json.dumps(meta) if meta else None))
            return int(cur.lastrowid)

    def list_events(self, limit: int = 100, since_id: int = 0) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?",
            (since_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def unread_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE read = 0").fetchone()["c"])

    def mark_events_read(self, up_to_id: Optional[int] = None) -> int:
        with self.conn:
            if up_to_id is None:
                cur = self.conn.execute("UPDATE events SET read = 1 WHERE read = 0")
            else:
                cur = self.conn.execute(
                    "UPDATE events SET read = 1 WHERE read = 0 AND id <= ?", (up_to_id,))
            return cur.rowcount

    def prune_events(self, days: int = 90) -> int:
        with self.conn:
            return self.conn.execute(
                "DELETE FROM events WHERE ts_utc < datetime('now', ?)",
                (f"-{int(days)} days",)).rowcount

    def add_web_action(self, action: str, target: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO web_actions (ts_utc, action, target) VALUES (?,?,?)",
                (utc_now_iso(), action, target))
            return int(cur.lastrowid)

    # ----- Probas §3 (une ligne par recalcul ; on lit la plus récente) ----
    def record_proba(self, symbol: str, timeframe: str, direction: str,
                     payload: dict) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO probas (ts_utc, symbol, timeframe, direction, payload) "
                "VALUES (?,?,?,?,?)",
                (utc_now_iso(), symbol, timeframe, direction, json.dumps(payload)))
            return int(cur.lastrowid)

    def latest_proba(self, symbol: str, timeframe: str,
                     direction: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT ts_utc, payload FROM probas WHERE symbol=? AND timeframe=? "
            "AND direction=? ORDER BY id DESC LIMIT 1",
            (symbol, timeframe, direction)).fetchone()
        if not row:
            return None
        d = json.loads(row["payload"])
        d["_ts_utc"] = row["ts_utc"]
        return d

    def all_latest_probas(self) -> list[dict[str, Any]]:
        """Dernier bloc §3 de chaque (symbol, timeframe, direction)."""
        rows = self.conn.execute(
            """SELECT p.symbol, p.timeframe, p.direction, p.ts_utc, p.payload
               FROM probas p JOIN (
                   SELECT symbol, timeframe, direction, MAX(id) AS mid FROM probas
                   GROUP BY symbol, timeframe, direction) g
               ON p.id = g.mid ORDER BY p.symbol, p.timeframe, p.direction""").fetchall()
        out = []
        for r in rows:
            out.append({"symbol": r["symbol"], "timeframe": r["timeframe"],
                        "direction": r["direction"], "ts_utc": r["ts_utc"],
                        "payload": json.loads(r["payload"])})
        return out

    def prune_probas(self, keep_per_key: int = 3) -> int:
        """Ne garde que les `keep_per_key` recalculs les plus récents par clé."""
        with self.conn:
            return self.conn.execute(
                """DELETE FROM probas WHERE id NOT IN (
                     SELECT id FROM (
                       SELECT id, ROW_NUMBER() OVER (
                         PARTITION BY symbol, timeframe, direction
                         ORDER BY id DESC) AS rn FROM probas)
                     WHERE rn <= ?)""", (keep_per_key,)).rowcount

    # ----- Tickets §3 (émis dès qu'un setup §2 est valide) ----------------
    def emit_ticket(self, symbol: str, timeframe: str, payload: dict) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO tickets (ts_utc, symbol, timeframe, payload) VALUES (?,?,?,?)",
                (utc_now_iso(), symbol, timeframe, json.dumps(payload)))
            return int(cur.lastrowid)

    def list_tickets(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id, ts_utc, symbol, timeframe, payload FROM tickets "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            out.append({"id": r["id"], "ts_utc": r["ts_utc"], "symbol": r["symbol"],
                        "timeframe": r["timeframe"], **json.loads(r["payload"])})
        return out

    def prune_tickets(self, days: int = 120) -> int:
        with self.conn:
            return self.conn.execute(
                "DELETE FROM tickets WHERE ts_utc < datetime('now', ?)",
                (f"-{int(days)} days",)).rowcount

    # ----- Santé / config -------------------------------------------------
    def record_health(self, cycle_kind: str, duration_s=None, cpu_s=None,
                      peak_mem_mb=None, clock_skew_s=None, note=None) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO health (ts_utc, cycle_kind, duration_s, cpu_s,
                   peak_mem_mb, clock_skew_s, note) VALUES (?,?,?,?,?,?,?)""",
                (utc_now_iso(), cycle_kind, duration_s, cpu_s, peak_mem_mb,
                 clock_skew_s, note))

    def set_kv(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO config_kv (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, value))

    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM config_kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
