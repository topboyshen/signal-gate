#!/usr/bin/env python3
"""Human-review Telegram bot for signal-gate (stdlib).

Modes (SIGNAL_GATE_REVIEW_MODE):
  http  — listen 127.0.0.1:8096 for forwarded callback_query from mini-hermes
          (required when TELEGRAM_DIGEST_* shares the mini-hermes bot token)
  poll  — long-poll getUpdates (only when this bot token is exclusive)

Approve posts HMAC envelope to Quant APPROVED_SIGNALS_URL; Reject updates DB only.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import logging
import os
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_outbox_spec = importlib.util.spec_from_file_location("review_review_outbox", Path(__file__).with_name("review_outbox.py"))
review_outbox = importlib.util.module_from_spec(_outbox_spec)
_outbox_spec.loader.exec_module(review_outbox)

BASE = Path(os.environ.get("SIGNAL_GATE_HOME", "/opt/signal-gate"))
DB_PATH = Path(os.environ.get("SIGNAL_GATE_DB", str(BASE / "data" / "signals.db")))
SECRETS_PATH = Path(os.environ.get("SIGNAL_GATE_SECRETS", str(BASE / "secrets.env")))
QUANT_ENV_PATH = Path(
    os.environ.get("SIGNAL_GATE_QUANT_ENV", str(BASE / "quant_approved.env"))
)
POLL_TIMEOUT = int(os.environ.get("SIGNAL_GATE_REVIEW_POLL", "25"))
OFFSET_FILE = Path(
    os.environ.get("SIGNAL_GATE_REVIEW_OFFSET", str(BASE / "data" / "review_offset.txt"))
)
REVIEW_MODE = (os.environ.get("SIGNAL_GATE_REVIEW_MODE") or "http").strip().lower()
HTTP_HOST = os.environ.get("SIGNAL_GATE_REVIEW_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("SIGNAL_GATE_REVIEW_PORT", "8096"))

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
log = logging.getLogger("signal-gate-review")


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def migrate_review_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(pending_signals)")}
    for name, col_type in REVIEW_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE pending_signals ADD COLUMN {name} {col_type}")
            log.info("db migrate: added column %s", name)


def ensure_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        migrate_review_columns(conn)
        review_outbox.ensure_schema(conn)
        conn.commit()


def parse_expires_at(value: str | None) -> datetime | None:
    return review_outbox.expiry(value)


def is_expired(expires_at: str | None, now: datetime | None = None) -> bool:
    dt = parse_expires_at(expires_at)
    if dt is None:
        return True  # unknown lifetime is never permission to execute
    now = now or datetime.now(timezone.utc)
    return now >= dt


def sign_body(secret: str, timestamp: str, raw_body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()


def post_to_quant(
    url: str,
    secret: str,
    x_source: str,
    payload: dict[str, Any],
    tls_verify: bool = True,
    timeout: float = 30.0,
) -> tuple[int, str]:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ts = str(int(time.time()))
    sig = sign_body(secret, ts, raw)
    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": sig,
            "X-Source": x_source,
        },
    )
    ctx: ssl.SSLContext | None = None
    if url.startswith("https://") and not tls_verify:
        ctx = ssl._create_unverified_context()  # noqa: S323
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"transport_error: {exc}"


def tg_api(
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 35.0,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def answer_callback(token: str, callback_id: str, text: str) -> None:
    try:
        tg_api(
            token,
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:180]},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("answerCallbackQuery failed: %s", exc)


def edit_message(token: str, chat_id: int | str, message_id: int, text: str) -> None:
    try:
        tg_api(
            token,
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:4000],
                "disable_web_page_preview": True,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("editMessageText failed: %s", exc)


def allowed_actor(
    chat_id: int | str,
    user_id: int | str,
    digest_chat_id: str,
    review_user_ids: set[str],
) -> bool:
    chat_s = str(chat_id)
    user_s = str(user_id)
    if review_user_ids:
        return user_s in review_user_ids
    return chat_s == str(digest_chat_id).strip() or user_s == str(digest_chat_id).strip()


def load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def save_offset(offset: int) -> None:
    try:
        OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        OFFSET_FILE.write_text(str(offset), encoding="utf-8")
    except OSError as exc:
        log.warning("failed to persist offset: %s", exc)


def fetch_row(conn: sqlite3.Connection, row_id: int) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM pending_signals WHERE id = ?", (row_id,)
    ).fetchone()
    return dict(row) if row else None


def apply_decision(
    conn: sqlite3.Connection,
    row_id: int,
    decision: str,
    reviewer: str,
    quant_cfg: dict[str, str],
) -> dict[str, Any]:
    """Review and durable downstream intent commit together, then claim delivery."""
    review_outbox.ensure_schema(conn)
    if decision not in ("approve", "reject"):
        raise ValueError("invalid decision")
    now = int(time.time())
    cur = conn.execute(
        """
        UPDATE pending_signals
        SET decision = ?, review_status = 'reviewed', reviewed_at = ?, reviewer = ?
        WHERE id = ? AND (decision IS NULL OR decision = '')
        """,
        (decision, now, reviewer, row_id),
    )
    if cur.rowcount == 0:
        existing = fetch_row(conn, row_id)
        conn.commit()
        return {
            "ok": False,
            "already": True,
            "row": existing,
            "message": "already reviewed",
        }
    row = fetch_row(conn, row_id)
    assert row is not None

    result: dict[str, Any] = {
        "ok": True,
        "already": False,
        "row": row,
        "decision": decision,
        "quant_status": None,
        "quant_http": None,
    }

    if decision != "approve":
        conn.commit()
        result["message"] = "rejected"
        return result

    if is_expired(row.get("expires_at")):
        conn.execute(
            """
            UPDATE pending_signals
            SET quant_status = ?, quant_response = ?
            WHERE id = ?
            """,
            ("skipped_expired", "expires_at is in the past; not posted to Quant", row_id),
        )
        conn.commit()
        result["quant_status"] = "skipped_expired"
        result["message"] = "approved but expired — not sent to Quant"
        return result

    try:
        envelope = json.loads(row["body_json"])
        if not isinstance(envelope, dict):
            envelope = {"raw": envelope}
    except (TypeError, json.JSONDecodeError):
        envelope = {"body_json": row["body_json"]}

    envelope.update(
        {
            "review_status": "reviewed",
            "decision": "approve",
            "reviewed_at": now,
            "reviewer": reviewer,
            "gate_id": row_id,
            "gate_source": row.get("source"),
            "idempotency_key": row.get("idempotency_key"),
            "expires_at": row.get("expires_at"),
        }
    )

    url = (quant_cfg.get("APPROVED_SIGNALS_URL") or "").strip()
    secret = (quant_cfg.get("HMAC_SECRET_DEDIROCK_TO_QUANT") or "").strip()
    review_outbox.enqueue(conn, "quant", row_id, envelope)

    if not url or not secret:
        conn.execute(
            """
            UPDATE pending_signals
            SET quant_status = ?, quant_response = ?
            WHERE id = ?
            """,
            ("config_missing", "APPROVED_SIGNALS_URL or HMAC secret missing", row_id),
        )
        conn.commit()
        result["quant_status"] = "config_missing"
        result["message"] = "approved but Quant config missing"
        return result

    conn.execute(
        "UPDATE pending_signals SET quant_status='pending',quant_response=NULL WHERE id=?", (row_id,)
    )
    conn.commit()
    result.update(deliver_quant(conn, row_id, quant_cfg) or {"quant_status": "pending", "message": "approved; durable delivery pending"})
    return result


def deliver_quant(conn, row_id, quant_cfg):
    url = (quant_cfg.get("APPROVED_SIGNALS_URL") or "").strip()
    secret = (quant_cfg.get("HMAC_SECRET_DEDIROCK_TO_QUANT") or "").strip()
    if not url or not secret:
        return {"quant_status": "config_missing", "message": "approved; durable delivery waiting for Quant config"}
    job = review_outbox.claim(conn, "quant", row_id)
    if job is None:
        return None
    envelope = json.loads(job["payload_json"])
    if is_expired(envelope.get("expires_at")):
        quant_status, status, body = "skipped_expired", None, "expiry unknown or elapsed before delivery"
        outcome = "expired"
    else:
        # The sending state is already committed. Any crash/transport exception
        # after this point is ambiguous and must not be blindly replayed.
        try:
            status, body = post_to_quant(url, secret, (quant_cfg.get("X_SOURCE") or "dedirock").strip(),
                envelope, tls_verify=(quant_cfg.get("TLS_VERIFY", "true").strip().lower() in ("1", "true", "yes", "on")))
        except Exception:
            status, body = 0, "transport failure; delivery unknown"
        outcome = "confirmed" if 200 <= status < 300 else "unknown"
        quant_status = "ok" if outcome == "confirmed" else "unknown"
    if not review_outbox.finish(conn, job, outcome, body, commit=False):
        conn.commit()
        return None
    conn.execute("UPDATE pending_signals SET quant_status=?,quant_response=? WHERE id=?",
                 (quant_status, str(body)[:2000], row_id))
    conn.commit()
    log.info("quant_post id=%s status=%s quant_status=%s", row_id, status, quant_status)
    return {"quant_status": quant_status, "quant_http": status,
            "message": "approved → Quant accepted" if quant_status == "ok" else "approved; Quant delivery " + quant_status}


def quant_worker(quant_cfg):
    with sqlite3.connect(DB_PATH) as conn:
        review_outbox.recover_sending(conn, "quant")
    while True:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                for row_id in review_outbox.pending_ids(conn, "quant"):
                    deliver_quant(conn, row_id, quant_cfg)
        except Exception as exc:
            log.error("quant_worker_error type=%s", type(exc).__name__)
        time.sleep(5)


def handle_callback(
    token: str,
    cq: dict[str, Any],
    digest_chat_id: str,
    review_user_ids: set[str],
    quant_cfg: dict[str, str],
) -> dict[str, Any]:
    data = str(cq.get("data") or "")
    callback_id = str(cq.get("id") or "")
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    message_id = msg.get("message_id")
    from_user = cq.get("from") or {}
    user_id = from_user.get("id")
    username = from_user.get("username") or from_user.get("first_name") or str(user_id)

    if not allowed_actor(chat_id, user_id, digest_chat_id, review_user_ids):
        log.warning(
            "reject callback from chat=%s user=%s data=%s", chat_id, user_id, data
        )
        if callback_id:
            answer_callback(token, callback_id, "not authorized")
        return {"ok": False, "error": "not_authorized"}

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "sg" or parts[1] not in ("ok", "no"):
        if callback_id:
            answer_callback(token, callback_id, "unknown action")
        return {"ok": False, "error": "unknown_action"}
    try:
        row_id = int(parts[2])
    except ValueError:
        if callback_id:
            answer_callback(token, callback_id, "bad id")
        return {"ok": False, "error": "bad_id"}

    decision = "approve" if parts[1] == "ok" else "reject"
    reviewer = f"{username} ({user_id})"

    with sqlite3.connect(DB_PATH) as conn:
        migrate_review_columns(conn)
        row = fetch_row(conn, row_id)
        if row is None:
            if callback_id:
                answer_callback(token, callback_id, "signal not found")
            return {"ok": False, "error": "not_found"}
        result = apply_decision(conn, row_id, decision, reviewer, quant_cfg)

    if result.get("already"):
        prev = (result.get("row") or {}).get("decision") or "?"
        if callback_id:
            answer_callback(token, callback_id, f"already reviewed ({prev})")
        if chat_id is not None and message_id is not None:
            edit_message(
                token,
                chat_id,
                int(message_id),
                (msg.get("text") or "") + f"\n\n— already reviewed ({prev})",
            )
        return result

    if callback_id:
        answer_callback(token, callback_id, result.get("message") or decision)
    summary_lines = [
        msg.get("text") or f"id={row_id}",
        "",
        f"decision: {decision}",
        f"reviewer: {reviewer}",
        f"at: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}",
    ]
    if decision == "approve":
        summary_lines.append(f"quant_status: {result.get('quant_status')}")
        if result.get("quant_http") is not None:
            summary_lines.append(f"quant_http: {result.get('quant_http')}")
    if chat_id is not None and message_id is not None:
        edit_message(token, chat_id, int(message_id), "\n".join(summary_lines))
    log.info(
        "reviewed id=%s decision=%s reviewer=%s quant=%s",
        row_id,
        decision,
        reviewer,
        result.get("quant_status"),
    )
    return result


def poll_loop(
    token: str,
    digest_chat_id: str,
    review_user_ids: set[str],
    quant_cfg: dict[str, str],
) -> None:
    offset = load_offset()
    log.info(
        "review bot polling getUpdates offset=%s chat_id=%s whitelist_users=%s",
        offset,
        digest_chat_id,
        sorted(review_user_ids) if review_user_ids else "(digest chat only)",
    )
    conflict_streak = 0
    while True:
        try:
            payload = {
                "timeout": POLL_TIMEOUT,
                "offset": offset,
                "allowed_updates": ["callback_query"],
            }
            resp = tg_api(token, "getUpdates", payload, timeout=POLL_TIMEOUT + 10)
            conflict_streak = 0
            if not resp.get("ok"):
                log.error("getUpdates not ok: %s", str(resp)[:300])
                time.sleep(3)
                continue
            for upd in resp.get("result") or []:
                upd_id = int(upd.get("update_id", 0))
                offset = max(offset, upd_id + 1)
                save_offset(offset)
                cq = upd.get("callback_query")
                if cq:
                    try:
                        handle_callback(
                            token, cq, digest_chat_id, review_user_ids, quant_cfg
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("callback handler failed update_id=%s", upd_id)
        except urllib.error.HTTPError as exc:
            body = exc.read()[:300]
            log.error("getUpdates HTTPError %s: %s", exc.code, body)
            if exc.code == 409:
                conflict_streak += 1
                if conflict_streak >= 3:
                    log.error(
                        "getUpdates conflict with another poller (likely mini-hermes). "
                        "Switch SIGNAL_GATE_REVIEW_MODE=http and enable hermes sg: forwarder."
                    )
                    time.sleep(30)
                else:
                    time.sleep(5)
            else:
                time.sleep(5)
        except Exception as exc:  # noqa: BLE001
            log.error("poll error: %s", exc)
            time.sleep(5)


class _State:
    token: str = ""
    digest_chat_id: str = ""
    review_user_ids: set[str] = set()
    quant_cfg: dict[str, str] = {}
    forward_secret: str = ""


class ForwardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

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
            self._send(200, {"ok": True, "service": "signal-gate-review", "mode": "http"})
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/hooks/v1/telegram-callback":
            self._send(404, {"error": "not_found"})
            return
        secret = (self.headers.get("X-Review-Forward-Secret") or "").strip()
        if not _State.forward_secret or not hmac.compare_digest(
            secret, _State.forward_secret
        ):
            self._send(401, {"error": "bad_forward_secret"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send(422, {"error": "bad_content_length"})
            return
        if length < 0 or length > 1_000_000:
            self._send(422, {"error": "body_too_large"})
            return
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(422, {"error": "invalid_json"})
            return
        # Accept either bare callback_query or {callback_query: {...}}
        cq = data.get("callback_query") if isinstance(data, dict) else None
        if cq is None and isinstance(data, dict) and data.get("data"):
            cq = data
        if not isinstance(cq, dict):
            self._send(422, {"error": "callback_query_required"})
            return
        try:
            result = handle_callback(
                _State.token,
                cq,
                _State.digest_chat_id,
                _State.review_user_ids,
                _State.quant_cfg,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("forwarded callback failed")
            self._send(500, {"error": "handler_failed", "detail": str(exc)})
            return
        self._send(200, {"status": "ok", "result": {
            k: result.get(k)
            for k in ("ok", "already", "decision", "quant_status", "quant_http", "message", "error")
            if k in result
        }})


def http_loop(
    token: str,
    digest_chat_id: str,
    review_user_ids: set[str],
    quant_cfg: dict[str, str],
    forward_secret: str,
) -> None:
    if not forward_secret:
        log.error("SIGNAL_GATE_REVIEW_FORWARD_SECRET required for http mode")
        raise SystemExit(1)
    _State.token = token
    _State.digest_chat_id = digest_chat_id
    _State.review_user_ids = review_user_ids
    _State.quant_cfg = quant_cfg
    _State.forward_secret = forward_secret
    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), ForwardHandler)
    log.info(
        "review bot http forward listener on %s:%s (mini-hermes must forward sg:* callbacks)",
        HTTP_HOST,
        HTTP_PORT,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()


def build_quant_cfg(secrets: dict[str, str], quant_env: dict[str, str]) -> dict[str, str]:
    cfg = dict(secrets)
    cfg.update(quant_env)
    if "TLS_VERIFY" not in cfg and "QUANT_TLS_VERIFY" in cfg:
        cfg["TLS_VERIFY"] = cfg["QUANT_TLS_VERIFY"]
    cfg.setdefault("X_SOURCE", "dedirock")
    return cfg


def main() -> int:
    secrets = load_env_file(SECRETS_PATH)
    quant_env = load_env_file(QUANT_ENV_PATH)
    # EnvFile from systemd may also inject
    for key in (
        "TELEGRAM_DIGEST_BOT_TOKEN",
        "TELEGRAM_DIGEST_CHAT_ID",
        "TELEGRAM_REVIEW_USER_IDS",
        "SIGNAL_GATE_REVIEW_FORWARD_SECRET",
        "APPROVED_SIGNALS_URL",
        "HMAC_SECRET_DEDIROCK_TO_QUANT",
        "TLS_VERIFY",
        "X_SOURCE",
    ):
        if os.environ.get(key) and key not in secrets:
            secrets[key] = os.environ[key]

    token = (secrets.get("TELEGRAM_DIGEST_BOT_TOKEN") or os.environ.get("TELEGRAM_DIGEST_BOT_TOKEN") or "").strip()
    chat_id = (secrets.get("TELEGRAM_DIGEST_CHAT_ID") or os.environ.get("TELEGRAM_DIGEST_CHAT_ID") or "").strip()
    if not token or not chat_id:
        log.error("TELEGRAM_DIGEST_BOT_TOKEN / TELEGRAM_DIGEST_CHAT_ID required")
        return 1

    review_ids_raw = (
        secrets.get("TELEGRAM_REVIEW_USER_IDS")
        or os.environ.get("TELEGRAM_REVIEW_USER_IDS")
        or ""
    ).strip()
    review_user_ids = {x.strip() for x in review_ids_raw.split(",") if x.strip()}
    forward_secret = (
        secrets.get("SIGNAL_GATE_REVIEW_FORWARD_SECRET")
        or os.environ.get("SIGNAL_GATE_REVIEW_FORWARD_SECRET")
        or ""
    ).strip()

    ensure_db(DB_PATH)
    quant_cfg = build_quant_cfg(secrets, quant_env)
    threading.Thread(target=quant_worker, args=(quant_cfg,), name="sg-quant-outbox", daemon=True).start()
    log.info(
        "mode=%s quant_target=%s tls_verify=%s x_source=%s",
        REVIEW_MODE,
        bool((quant_cfg.get("APPROVED_SIGNALS_URL") or "").strip()),
        quant_cfg.get("TLS_VERIFY", "true"),
        quant_cfg.get("X_SOURCE", "dedirock"),
    )

    if REVIEW_MODE == "poll":
        poll_loop(token, chat_id, review_user_ids, quant_cfg)
    else:
        http_loop(token, chat_id, review_user_ids, quant_cfg, forward_secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
