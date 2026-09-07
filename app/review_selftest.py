#!/usr/bin/env python3
"""Self-test: insert pending via local HMAC; mock Quant approve path; optional notify log check."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Import review helpers from sibling module
sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_bot  # noqa: E402

BASE = Path(os.environ.get("SIGNAL_GATE_HOME", "/opt/signal-gate"))
SECRETS = Path(os.environ.get("SIGNAL_GATE_SECRETS", str(BASE / "secrets.env")))
DB = Path(os.environ.get("SIGNAL_GATE_DB", str(BASE / "data" / "signals.db")))
HOST = os.environ.get("SIGNAL_GATE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SIGNAL_GATE_PORT", "8095"))
URL = f"http://{HOST}:{PORT}/hooks/v1/pending-signals"


def load_secrets(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def sign(secret: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()


def post_pending(body: dict, secret: str, source: str = "jd") -> tuple[int, dict]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    req = urllib.request.Request(
        URL,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": sign(secret, ts, raw),
            "X-Source": source,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            payload = {}
        return e.code, payload


class _MockQuant(BaseHTTPRequestHandler):
    last: dict | None = None
    last_headers: dict | None = None

    def log_message(self, *args):  # noqa: ANN002
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            _MockQuant.last = json.loads(raw.decode())
        except Exception:  # noqa: BLE001
            _MockQuant.last = {"_raw": raw.decode(errors="replace")}
        _MockQuant.last_headers = {
            "X-Source": self.headers.get("X-Source"),
            "X-Timestamp": self.headers.get("X-Timestamp"),
            "X-Signature": self.headers.get("X-Signature"),
        }
        body = b'{"status":"accepted","mock":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    secrets = load_secrets(SECRETS)
    secret = secrets["HMAC_SECRET_JD"]
    key = f"review-selftest-{int(time.time())}"
    body = {
        "schema_version": "1",
        "idempotency_key": key,
        "signal_type": "review_probe",
        "symbol": "000001.SZ",
        "side": "buy",
        "confidence": 0.42,
        "expires_at": "2099-01-01T00:00:00+08:00",
        "provenance": {"tester": "review_selftest"},
        "review_status": "pending",
    }

    code, resp = post_pending(body, secret, "jd")
    print(f"pending_insert: HTTP {code} id={resp.get('id')} key={resp.get('idempotency_key')}")
    if code != 200:
        print("FAIL: pending insert")
        return 1
    row_id = int(resp["id"])

    # Give notify worker a moment (Telegram may or may not be reachable)
    time.sleep(2)
    print("note: check journalctl -u signal-gate for notify_sent (network-dependent)")

    # Mock Quant server
    mock = HTTPServer(("127.0.0.1", 0), _MockQuant)
    mock_port = mock.server_address[1]
    t = threading.Thread(target=mock.serve_forever, daemon=True)
    t.start()
    mock_url = f"http://127.0.0.1:{mock_port}/hooks/v1/approved-signals"
    quant_cfg = {
        "APPROVED_SIGNALS_URL": mock_url,
        "HMAC_SECRET_DEDIROCK_TO_QUANT": "selftest-secret-not-real",
        "TLS_VERIFY": "true",
        "X_SOURCE": "dedirock",
    }

    with sqlite3.connect(DB) as conn:
        review_bot.migrate_review_columns(conn)
        result = review_bot.apply_decision(conn, row_id, "approve", "selftest (0)", quant_cfg)

    print(f"approve_result: {json.dumps({k: result[k] for k in result if k != 'row'}, default=str)}")
    ok = (
        result.get("ok")
        and result.get("quant_status") == "ok"
        and result.get("quant_http") == 200
        and _MockQuant.last is not None
        and _MockQuant.last.get("decision") == "approve"
        and (_MockQuant.last_headers or {}).get("X-Source") == "dedirock"
        and str((_MockQuant.last_headers or {}).get("X-Signature", "")).startswith("sha256=")
    )

    # Idempotency: second approve must be already
    with sqlite3.connect(DB) as conn:
        again = review_bot.apply_decision(conn, row_id, "approve", "selftest (0)", quant_cfg)
    print(f"second_click_already: {again.get('already')}")
    ok = ok and again.get("already") is True

    # Reject path on a fresh row (no Quant)
    key2 = f"review-selftest-rej-{int(time.time())}"
    body2 = dict(body)
    body2["idempotency_key"] = key2
    code2, resp2 = post_pending(body2, secret, "jd")
    if code2 != 200:
        print("FAIL: reject pending insert")
        return 1
    rid2 = int(resp2["id"])
    with sqlite3.connect(DB) as conn:
        rej = review_bot.apply_decision(conn, rid2, "reject", "selftest (0)", quant_cfg)
    print(f"reject_result decision={rej.get('decision')} quant={rej.get('quant_status')}")
    ok = ok and rej.get("ok") and rej.get("decision") == "reject" and rej.get("quant_status") is None

    mock.shutdown()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
