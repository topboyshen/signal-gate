#!/usr/bin/env python3
"""Insert pending, check notify path, exercise HTTP reject forward."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

SECRETS = Path("/opt/signal-gate/secrets.env")
DB = Path("/opt/signal-gate/data/signals.db")


def load_secrets() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def post_pending(secrets: dict[str, str], key: str) -> dict:
    body = {
        "schema_version": "1",
        "idempotency_key": key,
        "signal_type": "e2e_forward",
        "symbol": "600519.SH",
        "side": "buy",
        "confidence": 0.55,
        "expires_at": "2099-06-01T00:00:00+08:00",
        "provenance": {"tester": "e2e_forward_check"},
        "review_status": "pending",
    }
    raw = json.dumps(body, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    sig = "sha256=" + hmac.new(
        secrets["HMAC_SECRET_JD"].encode(), f"{ts}.".encode() + raw, hashlib.sha256
    ).hexdigest()
    req = urllib.request.Request(
        "http://127.0.0.1:8095/hooks/v1/pending-signals",
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": sig,
            "X-Source": "jd",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def forward_reject(secrets: dict[str, str], row_id: int) -> tuple[int, str]:
    payload = {
        "callback_query": {
            "id": f"fake-cb-{row_id}",
            "data": f"sg:no:{row_id}",
            "from": {"id": int(secrets["TELEGRAM_DIGEST_CHAT_ID"]), "username": "e2e"},
            "message": {
                "message_id": 1,
                "chat": {"id": int(secrets["TELEGRAM_DIGEST_CHAT_ID"])},
                "text": f"e2e id={row_id}",
            },
        }
    }
    raw = json.dumps(payload).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8096/hooks/v1/telegram-callback",
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Review-Forward-Secret": secrets["SIGNAL_GATE_REVIEW_FORWARD_SECRET"],
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def main() -> int:
    secrets = load_secrets()
    key = f"e2e-forward-{int(time.time())}"
    accepted = post_pending(secrets, key)
    print("pending", accepted)
    time.sleep(2)
    code, body = forward_reject(secrets, int(accepted["id"]))
    print("forward", code, body[:300])
    import sqlite3

    row = sqlite3.connect(DB).execute(
        "SELECT id, decision, review_status, quant_status FROM pending_signals WHERE id=?",
        (accepted["id"],),
    ).fetchone()
    print("db", row)
    ok = code == 200 and row and row[1] == "reject" and row[2] == "reviewed"
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
