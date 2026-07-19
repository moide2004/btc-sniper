"""Accès SQLite — LA source de vérité applicative (§7.1, §7.2).

Règle des deux processus, un seul écrivain (§7.1) :
  * le worker ÉCRIT (heartbeat, événements, santé, tables métier) ;
  * la web app LIT uniquement, SAUF la table dédiée `web_actions`
    (acquittement d'alarme, marquage lu/non-lu).

SQLite est ouvert en mode WAL : lectures concurrentes non bloquantes pendant
que le worker écrit. Chaque écriture est atomique par transaction.

En P1 seules les tables du socle sont réellement alimentées (heartbeat,
events, health, config). Les tables métier (probas, tickets, positions,
journal) sont créées dès maintenant pour figer la forme de la base ; elles
seront remplies en P2+.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import CONFIG


def utc_now_iso() -> str:
    """Horodatage UTC ISO-8601 à la seconde (§2.4 : UTC partout)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


_SCHEMA = """
-- Socle (P1) -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS heartbeat (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    beat_utc      TEXT    NOT NULL,      -- dernier battement (§7.3)
    cycle         INTEGER NOT NULL,      -- compteur de cycle
    source        TEXT    NOT NULL,      -- source de données active
    status        TEXT    NOT NULL       -- ok | degraded
);

CREATE TABLE IF NOT EXISTS events (       -- la cloche interne (§6.2)
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     TEXT    NOT NULL,
    kind       TEXT    NOT NULL,          -- data_incident | worker | candidate | ...
    level      TEXT    NOT NULL,          -- info | warning | error
    message    TEXT    NOT NULL,
    meta       TEXT,                      -- JSON optionnel
    read       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts_utc);

CREATE TABLE IF NOT EXISTS health (       -- budget ressources (§7.6)
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc        TEXT    NOT NULL,
    cycle_kind    TEXT    NOT NULL,       -- daily | worker
    duration_s    REAL,
    cpu_s         REAL,
    peak_mem_mb   REAL,
    clock_skew_s  REAL,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS config_kv (    -- config non secrète, état persistant
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Table d'ÉCRITURE autorisée depuis la web app (§7.1) -------------------------
CREATE TABLE IF NOT EXISTS web_actions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     TEXT    NOT NULL,
    action     TEXT    NOT NULL,          -- ack_alarm | mark_read
    target     TEXT
);

-- Tables métier (forme figée, remplies en P2+) -------------------------------
CREATE TABLE IF NOT EXISTS probas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     TEXT NOT NULL,
    timeframe  TEXT NOT NULL,
    state      TEXT NOT NULL,
    direction  TEXT NOT NULL,             -- long | short
    payload    TEXT NOT NULL              -- JSON : p̂, n, Wilson, EV, réalisme...
);
CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     TEXT NOT NULL,
    stage      TEXT NOT NULL,             -- 1h | 4h | 1D
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_utc TEXT NOT NULL,
    closed_utc TEXT,
    stage      TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     TEXT NOT NULL,
    stage      TEXT NOT NULL,
    payload    TEXT NOT NULL
);
"""


class Store:
    """Enveloppe fine autour d'une connexion SQLite en mode WAL."""

    def __init__(self, path: Optional[Path] = None, read_only: bool = False) -> None:
        self.path = Path(path) if path else CONFIG.db_path
        self.read_only = read_only
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if read_only:
            uri = f"file:{self.path}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
        else:
            self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        if not read_only:
            self.init_schema()

    def _apply_pragmas(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA foreign_keys=ON;")
        if not self.read_only:
            # Durabilité correcte sans le coût d'un fsync par écriture.
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA busy_timeout=30000;")
        cur.close()

    def init_schema(self) -> None:
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

    # ----- Heartbeat & reprise (§7.3) --------------------------------------
    def write_heartbeat(self, cycle: int, source: str, status: str = "ok") -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO heartbeat (id, beat_utc, cycle, source, status)
                   VALUES (1, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     beat_utc=excluded.beat_utc, cycle=excluded.cycle,
                     source=excluded.source, status=excluded.status""",
                (utc_now_iso(), cycle, source, status),
            )

    def get_heartbeat(self) -> Optional[dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM heartbeat WHERE id = 1").fetchone()
        return dict(row) if row else None

    def heartbeat_age_seconds(self) -> Optional[float]:
        hb = self.get_heartbeat()
        if not hb:
            return None
        beat = datetime.fromisoformat(hb["beat_utc"])
        if beat.tzinfo is None:  # tolère un horodatage naïf : supposé UTC
            beat = beat.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - beat).total_seconds()

    # ----- Cloche interne / événements (§6.2) ------------------------------
    def add_event(
        self, kind: str, level: str, message: str, meta: Optional[dict] = None
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO events (ts_utc, kind, level, message, meta) VALUES (?,?,?,?,?)",
                (utc_now_iso(), kind, level, message, json.dumps(meta) if meta else None),
            )
            return int(cur.lastrowid)

    def list_events(self, limit: int = 100, since_id: int = 0) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id DESC LIMIT ?",
            (since_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def unread_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM events WHERE read = 0").fetchone()
        return int(row["c"])

    def mark_events_read(self, up_to_id: Optional[int] = None) -> int:
        """Marque des événements comme lus. ÉCRITURE autorisée depuis la web app
        (§7.1) : journalisée dans web_actions."""
        with self.conn:
            if up_to_id is None:
                cur = self.conn.execute("UPDATE events SET read = 1 WHERE read = 0")
            else:
                cur = self.conn.execute(
                    "UPDATE events SET read = 1 WHERE read = 0 AND id <= ?", (up_to_id,))
            self.conn.execute(
                "INSERT INTO web_actions (ts_utc, action, target) VALUES (?,?,?)",
                (utc_now_iso(), "mark_read", str(up_to_id) if up_to_id else "all"))
            return cur.rowcount

    def prune_events(self, days: int = 90) -> int:
        """Rétention 90 jours (§6.2)."""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM events WHERE ts_utc < datetime('now', ?)",
                (f"-{int(days)} days",),
            )
            return cur.rowcount

    # ----- Santé / budget ressources (§7.6) --------------------------------
    def record_health(
        self,
        cycle_kind: str,
        duration_s: float | None = None,
        cpu_s: float | None = None,
        peak_mem_mb: float | None = None,
        clock_skew_s: float | None = None,
        note: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO health
                   (ts_utc, cycle_kind, duration_s, cpu_s, peak_mem_mb, clock_skew_s, note)
                   VALUES (?,?,?,?,?,?,?)""",
                (utc_now_iso(), cycle_kind, duration_s, cpu_s, peak_mem_mb, clock_skew_s, note),
            )

    # ----- Config persistante (clé/valeur) ---------------------------------
    def set_kv(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO config_kv (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value),
            )

    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM config_kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
