"""Durable review side effects; a claim is committed BEFORE any network call.

Only pending jobs may be claimed. Sending/unknown jobs require reconciliation,
never automatic replay. Migration deliberately does NOT enqueue historical rows.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime


def expiry(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None and stamp.utcoffset() is not None else None


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS review_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK(kind IN ('notify','quant')),
        signal_id INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
          CHECK(status IN ('pending','sending','confirmed','failed','unknown','expired')),
        claim_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
        message_id TEXT, response TEXT, UNIQUE(kind,signal_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_outbox_pending ON review_outbox(kind,status,id)")


def enqueue(conn, kind, signal_id, payload):
    """Caller commits this together with intake/review; no network or commit."""
    conn.execute("""INSERT INTO review_outbox(kind,signal_id,payload_json,created_at)
                    VALUES(?,?,?,?) ON CONFLICT(kind,signal_id) DO NOTHING""",
                 (kind, signal_id, json.dumps(payload, ensure_ascii=False), int(time.time())))


def get(conn, kind, signal_id):
    cursor = conn.execute("SELECT * FROM review_outbox WHERE kind=? AND signal_id=?", (kind, signal_id))
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip([column[0] for column in cursor.description], row))


def pending_ids(conn, kind, limit=100):
    return [row[0] for row in conn.execute(
        "SELECT signal_id FROM review_outbox WHERE kind=? AND status='pending' ORDER BY id LIMIT ?", (kind, limit))]


def claim(conn, kind, signal_id):
    """SQLite CAS serializes workers/processes; return only after durable claim."""
    claim_id = str(uuid.uuid4())
    cur = conn.execute("""UPDATE review_outbox SET status='sending',claim_id=?,
        attempts=attempts+1,started_at=? WHERE kind=? AND signal_id=? AND status='pending'""",
        (claim_id, int(time.time()), kind, signal_id))
    conn.commit()
    if cur.rowcount != 1:
        return None
    return get(conn, kind, signal_id)


def finish(conn, job, status, response="", message_id=None, *, commit=True):
    if status not in ("confirmed", "failed", "unknown", "expired"):
        raise ValueError("invalid outbox result")
    # An in-flight response may arrive after another worker's startup recovery.
    # The original claim can close unknown, but no process can reclaim it.
    cur = conn.execute("""UPDATE review_outbox SET status=?,completed_at=?,response=?,message_id=?
        WHERE id=? AND claim_id=? AND status IN ('sending','unknown')""",
        (status, int(time.time()), str(response)[:2000], str(message_id) if message_id is not None else None,
         job["id"], job["claim_id"]))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def recover_sending(conn, kind):
    """A lost ACK cannot prove non-delivery. Preserve job, don't resend it."""
    conn.execute("""UPDATE review_outbox SET status='unknown',response='process interrupted; reconcile before retry'
                    WHERE kind=? AND status='sending'""", (kind,))
    if kind == "quant":
        conn.execute("""UPDATE pending_signals SET quant_status='unknown',
            quant_response='durable sending interrupted; automatic resend prohibited'
            WHERE id IN (SELECT signal_id FROM review_outbox WHERE kind='quant' AND status='unknown')
              AND (quant_status IS NULL OR quant_status IN ('pending','sending','config_missing'))""")
    conn.commit()
