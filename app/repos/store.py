"""
Minimal SQLite store. stdlib sqlite3 -- no ORM, no migrations, no setup.

A judge clones the repo and runs one command. Every layer of infrastructure
between them and the dashboard is a chance for them to close the tab.

Holds four things:
  runs        one benchmark execution
  cases       one obligation under one arm
  decisions   the full record: snapshot in, trace + candidates + choice out
  events      the live webhook path (P6/P7)
  actions     scheduled work, with a UNIQUE idempotency key
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parents[2] / "razorrecovery.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, preset TEXT, n INTEGER, seed INTEGER,
  created_at TEXT, board JSON);

CREATE TABLE IF NOT EXISTS cases (
  case_id TEXT PRIMARY KEY, run_id TEXT, obligation_id TEXT, customer_id TEXT,
  arm TEXT, amount INTEGER, failure_class TEXT, method TEXT, kind TEXT,
  rung INTEGER, attempts INTEGER, status TEXT, contacts_sent INTEGER,
  actions_taken INTEGER, opened_at TEXT, closed_at TEXT);
CREATE INDEX IF NOT EXISTS ix_cases_run ON cases(run_id, arm, status);

CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY, run_id TEXT, case_id TEXT, decided_at TEXT,
  action TEXT, stop_reason TEXT, snapshot JSON, gate_trace JSON,
  candidates JSON, config_version TEXT, notes TEXT, highlight TEXT);
CREATE INDEX IF NOT EXISTS ix_dec_case ON decisions(case_id, decided_at);
CREATE INDEX IF NOT EXISTS ix_dec_hl ON decisions(run_id, highlight);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT UNIQUE NOT NULL,
  obligation_id TEXT, type TEXT, payload JSON, received_at TEXT);

CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY, case_id TEXT, obligation_id TEXT, type TEXT,
  execute_at TEXT, status TEXT, idem_key TEXT UNIQUE NOT NULL,
  attempts INTEGER DEFAULT 0, detail TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS obligations (
  id TEXT PRIMARY KEY, customer_id TEXT, amount_due INTEGER,
  amount_settled INTEGER DEFAULT 0, status TEXT, opened_at TEXT);
"""


def connect(path: str | Path = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def reset_run(con: sqlite3.Connection, run_id: str) -> None:
    for t in ("cases", "decisions"):
        con.execute(f"DELETE FROM {t} WHERE run_id = ?", (run_id,))
    con.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
    con.commit()


def save_run(con, run_id, preset, n, seed, created_at, board) -> None:
    con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                (run_id, preset, n, seed, created_at, json.dumps(board, default=str)))
    con.commit()


def save_cases(con, rows: list[tuple]) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()


def save_decisions(con, rows: list[tuple]) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()


# -- reads ----------------------------------------------------------------

def get_run(con, run_id: str) -> dict[str, Any] | None:
    r = con.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(r) if r else None


def list_runs(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT run_id, preset, n, seed, created_at FROM runs ORDER BY created_at DESC")]


def list_cases(con, run_id: str, arm: str | None = None, status: str | None = None,
               limit: int = 100, offset: int = 0) -> list[dict]:
    q = "SELECT * FROM cases WHERE run_id = ?"
    p: list[Any] = [run_id]
    if arm:
        q += " AND arm = ?"; p.append(arm)
    if status:
        q += " AND status = ?"; p.append(status)
    q += " ORDER BY amount DESC LIMIT ? OFFSET ?"
    p += [limit, offset]
    return [dict(r) for r in con.execute(q, p)]


def get_case(con, case_id: str) -> dict | None:
    r = con.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if not r:
        return None
    case = dict(r)
    case["decisions"] = [_decode(dict(d)) for d in con.execute(
        "SELECT * FROM decisions WHERE case_id = ? ORDER BY decided_at", (case_id,))]
    return case


def get_decision(con, decision_id: str) -> dict | None:
    r = con.execute("SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)).fetchone()
    return _decode(dict(r)) if r else None


def highlights(con, run_id: str, kind: str, limit: int = 20) -> list[dict]:
    return [_decode(dict(r)) for r in con.execute(
        "SELECT * FROM decisions WHERE run_id = ? AND highlight = ? LIMIT ?",
        (run_id, kind, limit))]


def _decode(d: dict) -> dict:
    for k in ("snapshot", "gate_trace", "candidates"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    return d
