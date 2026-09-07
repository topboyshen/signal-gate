#!/usr/bin/env python3
"""HMAC self-test against local signal-gate (valid→200, bad→401, dup→409)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(os.environ.get("SIGNAL_GATE_HOME", "/opt/signal-gate"))
SECRETS = Path(os.environ.get("SIGNAL_GATE_SECRETS", str(BASE / "secrets.env")))
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


def post(body: dict, secret: str, source: str, bad_sig: bool = False) -> tuple[int, str]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    sig = sign(secret, ts, raw)
    if bad_sig:
        sig = "sha256=" + ("0" * 64)
    req = urllib.request.Request(
        URL,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": sig,
            "X-Source": source,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    secrets = load_secrets(SECRETS)
    secret = secrets["HMAC_SECRET_JD"]
    key = f"selftest-{int(time.time())}"
    body = {
        "schema_version": "1",
        "idempotency_key": key,
        "signal_type": "probe",
        "symbol": "000001.SZ",
        "confidence": 0.5,
        "expires_at": "2099-01-01T00:00:00+08:00",
        "provenance": {"tester": "selftest"},
        "review_status": "pending",
    }
    results = []
    code, text = post(body, secret, "jd")
    results.append(("valid", code, text[:200]))
    code2, text2 = post(body, secret, "jd", bad_sig=True)
    results.append(("bad_sig", code2, text2[:200]))
    code3, text3 = post(body, secret, "jd")
    results.append(("duplicate", code3, text3[:200]))

    ok = results[0][1] == 200 and results[1][1] == 401 and results[2][1] == 409
    for name, code, text in results:
        print(f"{name}: HTTP {code} {text}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
