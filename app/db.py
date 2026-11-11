"""SQLite storage, stdlib only. One connection, one re-entrant lock, so the web
handlers, the poller and the sweeper can share it safely."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from . import config

_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL,
  rule_name TEXT NOT NULL,
  severity TEXT,
  status TEXT NOT NULL,                -- new | waiting | paged | resolved
  chat_id TEXT,
  alert_message_id TEXT,               -- the MonitorFlow message in the alert group
  thread_root_message_id TEXT,         -- message we reply under
  thread_id TEXT,                      -- Lark topic id (omt_...), if Lark returned one
  check_person_open_id TEXT,
  wait_deadline REAL,                  -- unix seconds; set only while status = waiting
  page_attempts INTEGER DEFAULT 0,
  pd_dedup_key TEXT,
  policy_json TEXT,
  alert_json TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_incidents_fp_status ON incidents(fingerprint, status);

CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL,
  stage TEXT NOT NULL,                 -- policy | ai | reply | timeout | pagerduty
  decision TEXT NOT NULL,
  reasoning TEXT,
  inputs_json TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_id INTEGER NOT NULL,
  result TEXT NOT NULL,                -- human_normal | human_escalate | timeout_paged | ai_escalate | ...
  actor TEXT,
  detail TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_messages (
  message_id TEXT PRIMARY KEY,
  created_at REAL NOT NULL
);

-- One Lark card per PagerDuty incident. Every PagerDuty state (from Zoho Flow) and every
-- button click repaints this same card instead of posting a new one.
CREATE TABLE IF NOT EXISTS pd_cards (
  pd_incident_id TEXT PRIMARY KEY,     -- PagerDuty incident id (Q...)
  lark_message_id TEXT,                -- the card message we PATCH (om_...)
  chat_id TEXT,
  state TEXT,                          -- triggered | escalated | acknowledged | snoozed | resolved
  title TEXT,
  html_url TEXT,
  urgency TEXT,
  service TEXT,
  assignees TEXT,
  history_json TEXT,                   -- [{"state":..., "line":...}, ...] oldest first, shown on the card
  incident_id INTEGER,                 -- our incidents.id when the PagerDuty incident came from us
  ai_notes_json TEXT,                  -- {"triggered": "...", "resolved": "..."} AI note per state
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
"""

# Columns added after a table already existed on someone's disk: (table, column, type)
_MIGRATIONS = [
    ("pd_cards", "incident_id", "INTEGER"),
    ("pd_cards", "ai_notes_json", "TEXT"),
    ("pd_cards", "meeting_url", "TEXT"),    # Lark meeting link created from the card (acknowledged state)
    ("pd_cards", "meeting_reserve_id", "TEXT"),  # to cancel the reservation when the incident resolves
]


def init() -> None:
    global _CONN
    with _LOCK:
        if _CONN is None:
            _CONN = sqlite3.connect(config.DATABASE_PATH, check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            # WAL: readers never block the writer and the sqlite3 CLI can inspect/fix rows while the bot runs
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.execute("PRAGMA busy_timeout=5000")
            _CONN.executescript(SCHEMA)
            for table, column, ctype in _MIGRATIONS:
                have = {r[1] for r in _CONN.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in have:
                    _CONN.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")
            _CONN.commit()


def _conn() -> sqlite3.Connection:
    if _CONN is None:
        init()
    assert _CONN is not None
    return _CONN


def _one(cur: sqlite3.Cursor) -> Optional[Dict[str, Any]]:
    row = cur.fetchone()
    return dict(row) if row else None


def _all(cur: sqlite3.Cursor) -> List[Dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


# --- idempotency ---------------------------------------------------------------------

def mark_processed(message_id: str) -> bool:
    """True if this message is new (and is now recorded). False = already handled."""
    with _LOCK:
        # INSERT OR IGNORE, not try/except IntegrityError: Python's sqlite3 opens a transaction
        # before the INSERT, and a failed INSERT left it open, holding the write lock until some
        # other call committed. The poller hits duplicates every minute, so the sqlite3 CLI saw
        # "database is locked" most of the time.
        cur = _conn().execute("INSERT OR IGNORE INTO processed_messages VALUES (?, ?)", (message_id, time.time()))
        _conn().commit()
        return cur.rowcount == 1


# --- incidents -----------------------------------------------------------------------

def create_incident(**fields: Any) -> Dict[str, Any]:
    now = time.time()
    fields.setdefault("status", "new")
    fields["created_at"] = now
    fields["updated_at"] = now
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with _LOCK:
        cur = _conn().execute(f"INSERT INTO incidents ({cols}) VALUES ({marks})", tuple(fields.values()))
        _conn().commit()
        inc = get_incident(int(cur.lastrowid))
    assert inc is not None
    return inc


def update_incident(incident_id: int, **fields: Any) -> None:
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _LOCK:
        _conn().execute(f"UPDATE incidents SET {sets} WHERE id = ?", (*fields.values(), incident_id))
        _conn().commit()


def get_incident(incident_id: int) -> Optional[Dict[str, Any]]:
    with _LOCK:
        return _one(_conn().execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)))


def find_open_incident(fingerprint: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        return _one(_conn().execute(
            "SELECT * FROM incidents WHERE fingerprint = ? AND status != 'resolved' ORDER BY id DESC LIMIT 1",
            (fingerprint,),
        ))


def find_incident_by_thread(ids: List[str]) -> Optional[Dict[str, Any]]:
    """Match a reply to its incident by root message id or Lark thread id."""
    ids = [i for i in ids if i]
    if not ids:
        return None
    marks = ", ".join("?" for _ in ids)
    with _LOCK:
        return _one(_conn().execute(
            f"SELECT * FROM incidents WHERE status != 'resolved' AND "
            f"(thread_root_message_id IN ({marks}) OR thread_id IN ({marks}) OR alert_message_id IN ({marks})) "
            f"ORDER BY id DESC LIMIT 1",
            (*ids, *ids, *ids),
        ))


def find_open_incident_any() -> bool:
    """True if any incident of ours is paged or waiting. Cheap guard before calling PagerDuty."""
    with _LOCK:
        return _one(_conn().execute("SELECT id FROM incidents WHERE status IN ('paged','waiting') LIMIT 1")) is not None


def find_incident_by_dedup(dedup_key: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        return _one(_conn().execute(
            "SELECT * FROM incidents WHERE pd_dedup_key = ? ORDER BY id DESC LIMIT 1", (dedup_key,)
        ))


def due_waiting(now: float) -> List[Dict[str, Any]]:
    with _LOCK:
        return _all(_conn().execute(
            "SELECT * FROM incidents WHERE status = 'waiting' AND wait_deadline IS NOT NULL AND wait_deadline <= ?",
            (now,),
        ))


# --- PagerDuty cards -----------------------------------------------------------------

def get_pd_card(pd_incident_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        return _one(_conn().execute("SELECT * FROM pd_cards WHERE pd_incident_id = ?", (pd_incident_id,)))


def upsert_pd_card(pd_incident_id: str, **fields: Any) -> Dict[str, Any]:
    """Insert the card row or update the given fields (None values are left untouched)."""
    now = time.time()
    fields = {k: v for k, v in fields.items() if v is not None}
    with _LOCK:
        if get_pd_card(pd_incident_id) is None:
            fields.setdefault("history_json", "[]")
            cols = ", ".join(["pd_incident_id", *fields, "created_at", "updated_at"])
            marks = ", ".join("?" for _ in range(len(fields) + 3))
            _conn().execute(f"INSERT INTO pd_cards ({cols}) VALUES ({marks})", (pd_incident_id, *fields.values(), now, now))
        elif fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            _conn().execute(f"UPDATE pd_cards SET {sets}, updated_at = ? WHERE pd_incident_id = ?",
                            (*fields.values(), now, pd_incident_id))
        _conn().commit()
        card = get_pd_card(pd_incident_id)
    assert card is not None
    return card


# --- audit trail ---------------------------------------------------------------------

def log_decision(incident_id: int, stage: str, decision: str, reasoning: str = "",
                 inputs: Optional[Dict[str, Any]] = None) -> None:
    with _LOCK:
        _conn().execute(
            "INSERT INTO decisions (incident_id, stage, decision, reasoning, inputs_json, created_at) VALUES (?,?,?,?,?,?)",
            (incident_id, stage, decision, reasoning, json.dumps(inputs or {}, ensure_ascii=False), time.time()),
        )
        _conn().commit()


def log_outcome(incident_id: int, result: str, actor: str = "", detail: str = "") -> None:
    with _LOCK:
        _conn().execute(
            "INSERT INTO outcomes (incident_id, result, actor, detail, created_at) VALUES (?,?,?,?,?)",
            (incident_id, result, actor, detail, time.time()),
        )
        _conn().commit()


def past_cases(rule_name: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Latest outcome of recent resolved incidents for the same rule. This is the
    "past cases" input to AI triage (simple version; embeddings come later)."""
    with _LOCK:
        return _all(_conn().execute(
            """
            SELECT i.id, i.severity, i.created_at, o.result, o.detail
            FROM incidents i
            LEFT JOIN outcomes o
              ON o.id = (SELECT id FROM outcomes WHERE incident_id = i.id ORDER BY id DESC LIMIT 1)
            WHERE i.rule_name = ? AND i.status = 'resolved'
            ORDER BY i.id DESC LIMIT ?
            """,
            (rule_name, limit),
        ))
