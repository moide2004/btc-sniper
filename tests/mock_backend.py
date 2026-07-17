"""
mock_backend.py — Backend en mémoire, sans réseau, pour tester crm.py.
Émule la surface d'API utilisée par CRM (ws/get_all_values, read_records,
append_row, write_rows, sheet_id, batch/deleteDimension).
"""
from __future__ import annotations

import re


def a1_to_rc(a1: str) -> tuple[int, int]:
    m = re.match(r"([A-Z]+)(\d+)", a1)
    letters, row = m.group(1), int(m.group(2))
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - 64)
    return row, col  # 1-based


class _WS:
    def __init__(self, grid: list[list]):
        self.grid = grid

    def get_all_values(self):
        return [list(map(str, r)) for r in self.grid]


class MockBackend:
    def __init__(self, headers_by_tab: dict[str, list[str]]):
        self.grids: dict[str, list[list]] = {
            t: [list(h)] for t, h in headers_by_tab.items()
        }
        self._ids = {t: i for i, t in enumerate(headers_by_tab)}

    def ws(self, title):
        return _WS(self.grids[title])

    def sheet_id(self, title):
        return self._ids[title]

    def read_records(self, title):
        g = self.grids[title]
        if len(g) < 2:
            return []
        headers = g[0]
        out = []
        for r in g[1:]:
            r = list(r) + [""] * (len(headers) - len(r))
            out.append({h: str(v) for h, v in zip(headers, r)})
        return out

    def append_row(self, title, row, raw=False):
        headers = self.grids[title][0]
        row = list(row) + [""] * (len(headers) - len(row))
        self.grids[title].append([str(v) for v in row])

    def write_rows(self, title, a1, rows, raw=False):
        r0, c0 = a1_to_rc(a1)
        g = self.grids[title]
        for dr, row in enumerate(rows):
            r = r0 + dr
            while len(g) < r:
                g.append([""] * len(g[0]))
            for dc, val in enumerate(row):
                c = c0 + dc
                while len(g[r - 1]) < c:
                    g[r - 1].append("")
                g[r - 1][c - 1] = str(val)

    def batch(self, requests):
        for req in requests:
            if "deleteDimension" in req:
                rng = req["deleteDimension"]["range"]
                title = next(t for t, i in self._ids.items()
                             if i == rng["sheetId"])
                start, end = rng["startIndex"], rng["endIndex"]
                del self.grids[title][start:end]
