#!/usr/bin/env python3
"""Minimal signal-gate HTTP service: POST /hooks/v1/pending-signals with HMAC auth."""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

_history_spec = importlib.util.spec_from_file_location("receiver_news_history", Path(__file__).with_name("news_history.py"))
assert _history_spec is not None and _history_spec.loader is not None
news_history = importlib.util.module_from_spec(_history_spec)
_history_spec.loader.exec_module(news_history)

BASE = Path(os.environ.get("SIGNAL_GATE_HOME", "/opt/signal-gate"))
DB_PATH = Path(os.environ.get("SIGNAL_GATE_DB", str(BASE / "data" / "signals.db")))
SECRETS_PATH = Path(os.environ.get("SIGNAL_GATE_SECRETS", str(BASE / "secrets.env")))
LISTEN_HOST = os.environ.get("SIGNAL_GATE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("SIGNAL_GATE_PORT", "8095"))
MAX_SKEW_SEC = int(os.environ.get("SIGNAL_GATE_MAX_SKEW", "300"))

REQUIRED_FIELDS = (
    "schema_version",
    "idempotency_key",
    "signal_type",
    "symbol",
    "confidence",
    "expires_at",
    "provenance",
    "review_status",
)

SOURCE_SECRET_KEYS = {
    "jd": "HMAC_SECRET_JD",
    "nng8": "HMAC_SECRET_NNG8",
    "quant": "HMAC_SECRET_QUANT",
}

REVIEW_COLUMNS = (
    ("decision", "TEXT"),
    ("reviewed_at", "INTEGER"),
    ("reviewer", "TEXT"),
    ("quant_status", "TEXT"),
    ("quant_response", "TEXT"),
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("signal-gate")

_notify_q: Queue[dict[str, Any]] = Queue()
_notify_thread: threading.Thread | None = None


def load_secrets(path: Path) -> dict[str, str]:
    secrets: dict[str, str] = {}
    if not path.is_file():
        raise FileNotFoundError(f"secrets file missing: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        secrets[key.strip()] = value.strip().strip('"').strip("'")
    return secrets


def migrate_review_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(pending_signals)")}
    for name, col_type in REVIEW_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE pending_signals ADD COLUMN {name} {col_type}")
            log.info("db migrate: added column %s", name)


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                confidence REAL,
                expires_at TEXT,
                provenance TEXT,
                schema_version TEXT,
                review_status TEXT NOT NULL,
                body_json TEXT NOT NULL,
                received_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pending_received
            ON pending_signals(received_at)
            """
        )
        migrate_review_columns(conn)
        conn.commit()


def safe_source_label(value: str) -> str:
    """Loggable source token only; never echo signatures or raw headers."""
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    if value and 0 < len(value) <= 32 and all(char in allowed for char in value):
        return value
    return "invalid"


def verify_hmac(secret: str, timestamp: str, raw_body: bytes, signature: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def format_review_message(row: dict[str, Any]) -> str:
    side = row.get("side")
    side_s = "n/a" if side is None else str(side)
    lines = [
        "🛡 Signal Gate — pending review",
        f"id: {row['id']}",
        f"type: {row.get('signal_type')}",
        f"symbol: {row.get('symbol')}",
        f"side: {side_s}",
        f"confidence: {row.get('confidence')}",
        f"expires_at: {row.get('expires_at')}",
        f"source: {row.get('source')}",
        f"key: {row.get('idempotency_key')}",
    ]
    return "\n".join(lines)


def send_review_notify(token: str, chat_id: str, row: dict[str, Any]) -> bool:
    """Send Telegram review prompt with Approve/Reject inline buttons. callback_data uses DB id."""
    text = format_review_message(row)
    row_id = int(row["id"])
    # Keep callback_data well under Telegram's 64-byte limit.
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"sg:ok:{row_id}"},
                {"text": "❌ Reject", "callback_data": f"sg:no:{row_id}"},
            ]
        ]
    }
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "reply_markup": keyboard,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read()
            ok = 200 <= resp.status < 300
            if ok:
                log.info(
                    "notify_sent id=%s idem=%s status=%s bytes=%s",
                    row_id,
                    row.get("idempotency_key"),
                    resp.status,
                    len(raw),
                )
            else:
                log.error("notify_failed id=%s status=%s", row_id, resp.status)
            return ok
    except HTTPError as exc:
        body = exc.read()[:300]
        log.error("notify_failed id=%s HTTPError %s: %s", row_id, exc.code, body)
        return False
    except (URLError, TimeoutError, OSError) as exc:
        log.error("notify_failed id=%s: %s", row_id, exc)
        return False


def _notify_worker(secrets: dict[str, str]) -> None:
    token = (secrets.get("TELEGRAM_DIGEST_BOT_TOKEN") or "").strip()
    chat_id = (secrets.get("TELEGRAM_DIGEST_CHAT_ID") or "").strip()
    if not token or not chat_id:
        log.warning(
            "review notify disabled (missing TELEGRAM_DIGEST_BOT_TOKEN/CHAT_ID); "
            "draining queue without send"
        )
        while True:
            try:
                _notify_q.get(timeout=3600)
            except Empty:
                continue
            _notify_q.task_done()
        return

    while True:
        row = _notify_q.get()
        try:
            send_review_notify(token, chat_id, row)
        except Exception:  # noqa: BLE001
            log.exception("notify worker crash on id=%s", row.get("id"))
        finally:
            _notify_q.task_done()


def start_notify_worker(secrets: dict[str, str]) -> None:
    global _notify_thread
    if _notify_thread and _notify_thread.is_alive():
        return
    _notify_thread = threading.Thread(
        target=_notify_worker,
        args=(secrets,),
        name="sg-review-notify",
        daemon=True,
    )
    _notify_thread.start()
    log.info("review notify worker started")


def enqueue_review_notify(row: dict[str, Any]) -> None:
    _notify_q.put(row)


class Handler(BaseHTTPRequestHandler):
    secrets: dict[str, str] = {}
    db_path: Path = DB_PATH

    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), format % args)

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/healthz", "/health"):
            self._send(200, {"ok": True, "service": "signal-gate"})
            return
        self._send(404, {"error": "not_found"})

    def _auth_body(self) -> tuple[str, bytes] | tuple[None, None]:
        """Return (source, raw_body) or send error and (None, None)."""
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type != "application/json":
            self._send(422, {"error": "content_type_must_be_application_json"})
            return None, None
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send(422, {"error": "bad_content_length"})
            return None, None
        if length < 0 or length > 1_000_000:
            self._send(422, {"error": "body_too_large"})
            return None, None
        raw_body = self.rfile.read(length)
        source = (self.headers.get("X-Source") or "").strip()
        timestamp = (self.headers.get("X-Timestamp") or "").strip()
        signature = (self.headers.get("X-Signature") or "").strip()
        path = urlparse(self.path).path

        def reject(error: str):
            log.warning(
                "auth_failed path=%s error=%s source=%s ts_digits=%s sig=%s body=%s",
                path, error, safe_source_label(source),
                str(timestamp).isdigit(), bool(signature.startswith("sha256=")), length,
            )
            self._send(401, {"error": error})
            return None, None

        secret_key = SOURCE_SECRET_KEYS.get(source)
        if not secret_key or secret_key not in self.secrets:
            return reject("bad_source")
        secret = self.secrets[secret_key]
        if not secret:
            return reject("bad_source")
        try:
            ts = int(timestamp)
        except ValueError:
            return reject("bad_timestamp")
        if abs(int(time.time()) - ts) > MAX_SKEW_SEC:
            return reject("timestamp_skew")
        if not verify_hmac(secret, timestamp, raw_body, signature):
            return reject("bad_signature")
        return source, raw_body

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/hooks/v1/digest-drop":
            self._handle_digest_drop()
            return
        if path != "/hooks/v1/pending-signals":
            self._send(404, {"error": "not_found"})
            return

        source, raw_body = self._auth_body()
        if source is None or raw_body is None:
            return

        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(422, {"error": "invalid_json"})
            return
        if not isinstance(data, dict):
            self._send(422, {"error": "body_must_be_object"})
            return

        missing = [f for f in REQUIRED_FIELDS if f not in data]
        if missing:
            self._send(422, {"error": "missing_fields", "fields": missing})
            return
        if data.get("review_status") != "pending":
            self._send(422, {"error": "review_status_must_be_pending"})
            return

        idem = data.get("idempotency_key")
        if (not isinstance(idem, str) or not idem.strip() or not idem.isprintable()
                or idem.strip().lower().startswith("digest-")
                or len(idem.encode("utf-8")) > 240
                or any(char in idem for char in ("/", "\\", "\x00"))):
            self._send(422, {"error": "bad_idempotency_key"})
            return

        now = int(time.time())
        row_id: int | None = None
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    """
                    INSERT INTO pending_signals (
                        idempotency_key, source, signal_type, symbol, confidence,
                        expires_at, provenance, schema_version, review_status,
                        body_json, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idem.strip(),
                        source,
                        str(data.get("signal_type")),
                        str(data.get("symbol")),
                        data.get("confidence"),
                        str(data.get("expires_at")),
                        json.dumps(data.get("provenance"), ensure_ascii=False)
                        if not isinstance(data.get("provenance"), str)
                        else data.get("provenance"),
                        str(data.get("schema_version")),
                        "pending",
                        json.dumps(data, ensure_ascii=False),
                        now,
                    ),
                )
                if cur.lastrowid is None:
                    raise sqlite3.DatabaseError("missing inserted row id")
                row_id = cur.lastrowid
                conn.commit()
        except sqlite3.IntegrityError:
            self._send(409, {"error": "duplicate_idempotency_key", "idempotency_key": idem})
            return
        except sqlite3.Error as exc:
            log.exception("db error")
            self._send(500, {"error": "db_error", "detail": str(exc)})
            return

        inbox = BASE / "inbox" / source
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            stub = inbox / f"{idem.strip()}.json"
            stub.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            log.warning("failed to write inbox stub for %s", idem)

        if row_id is not None:
            enqueue_review_notify(
                {
                    "id": row_id,
                    "idempotency_key": idem.strip(),
                    "source": source,
                    "signal_type": data.get("signal_type"),
                    "symbol": data.get("symbol"),
                    "side": data.get("side"),
                    "confidence": data.get("confidence"),
                    "expires_at": data.get("expires_at"),
                }
            )

        self._send(
            200,
            {
                "status": "accepted",
                "id": row_id,
                "idempotency_key": idem.strip(),
                "source": source,
                "received_at": now,
            },
        )

    def _handle_digest_drop(self) -> None:
        source, raw_body = self._auth_body()
        if source is None or raw_body is None:
            return
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(422, {"error": "invalid_json"})
            return
        if not isinstance(data, dict):
            self._send(422, {"error": "body_must_be_object"})
            return
        # A sender-controlled date must never address receiver-owned receipts.
        # Full ISO source timestamps still map to their calendar-day alias.
        value = str(data.get("as_of") or data.get("date") or time.strftime("%Y-%m-%d"))
        try:
            day = date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            self._send(422, {"error": "bad_digest_date"})
            return
        if source == "jd":
            try:
                receipt = news_history.capture(BASE, raw_body, day)
            except (OSError, ValueError, UnicodeError):
                log.error("digest_drop storage_failed source=jd")
                self._send(500, {"error": "storage_unavailable"})
                return
            log.info("digest_drop source=jd day=%s bytes=%s received_at=%s body_sha256=%s",
                     day, len(raw_body), receipt["received_at"], receipt["body_sha256"])
            self._send(200, {"status": "accepted", "source": source, "day": day,
                             "path": str(BASE / "inbox" / source / f"digest-{day}.json"),
                             "received_at": receipt["received_at"], "body_sha256": receipt["body_sha256"]})
            return
        inbox = BASE / "inbox" / source
        inbox.mkdir(parents=True, exist_ok=True)
        # Keep one latest + dated file
        latest = inbox / "digest-latest.json"
        dated = inbox / f"digest-{day}.json"
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        latest.write_text(payload, encoding="utf-8")
        dated.write_text(payload, encoding="utf-8")
        # Optional markdown twin
        md = data.get("markdown") or data.get("text")
        if isinstance(md, str) and md.strip():
            (inbox / "digest-latest.md").write_text(md, encoding="utf-8")
            (inbox / f"digest-{day}.md").write_text(md, encoding="utf-8")
        log.info("digest_drop source=%s day=%s bytes=%s", source, day, len(payload))
        self._send(
            200,
            {"status": "accepted", "source": source, "day": day, "path": str(dated)},
        )


def main() -> None:
    secrets = load_secrets(SECRETS_PATH)
    init_db(DB_PATH)
    Handler.secrets = secrets
    Handler.db_path = DB_PATH
    start_notify_worker(secrets)
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    log.info("signal-gate listening on %s:%s db=%s", LISTEN_HOST, LISTEN_PORT, DB_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
