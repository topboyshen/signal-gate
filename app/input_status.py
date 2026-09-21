#!/usr/bin/env python3
"""Private, bounded status snapshot for the portal; no delivery or business writes.

Run as signal-gate inside project-portal-inputs.service. The systemd sandbox
denies network, credentials and all writes except this separate snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

OUTPUT = Path("/var/lib/project-portal-inputs/status.json")
COMMAND = ("/usr/bin/python3", "-B", "/opt/signal-gate/app/digest.py", "--dry-run", "--json")
TZ = ZoneInfo("Asia/Shanghai")
MAX_BYTES = 32768
SAFE_ISSUE = re.compile(
    r"(nng8|jd|quant|review):(missing|stale|unknown_date|future|invalid|schema_error|"
    r"too_large|truncated|io_error|expiry_unknown|placeholder)\Z")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("invalid constant")


def unknown(now: datetime) -> dict:
    return {"schema_version": 1, "report_date": now.astimezone(TZ).date().isoformat(),
            "state": "unknown", "checked_at": now.isoformat(), "quality_issues": []}


def normalize(raw: bytes, now: datetime) -> dict:
    """Only source/date/check status is eligible for publication, never raw output."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError("invalid output")
    value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    day = now.astimezone(TZ).date().isoformat()
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1 or value.get("status") != "dry_run"
            or value.get("report_date") != day or type(value.get("degraded")) is not bool):
        raise ValueError("invalid result")
    issues = value.get("issues")
    if (not isinstance(issues, list) or len(issues) > 64
            or any(not isinstance(code, str) or not SAFE_ISSUE.fullmatch(code) for code in issues)
            or value["degraded"] != bool(issues)):
        raise ValueError("invalid issues")
    generated = value.get("generated_at")
    if not isinstance(generated, str) or len(generated) > 48:
        raise ValueError("invalid check time")
    checked = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    if (checked.utcoffset() is None or checked.astimezone(TZ).date().isoformat() != day
            or not -60 <= (now - checked).total_seconds() <= 900):
        raise ValueError("outdated check")
    return {"schema_version": 1, "report_date": day, "checked_at": checked.isoformat(),
            "state": "issues" if issues else "clear", "quality_issues": sorted(set(issues))}


def collect(*, now: datetime | None = None, runner=None) -> dict:
    """Fixed read-only CLI and sanitized environment; errors never include stderr."""
    runner = subprocess.run if runner is None else runner
    try:
        result = runner(COMMAND, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, timeout=20, check=False,
                        cwd="/opt/signal-gate", env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                        "PYTHONDONTWRITEBYTECODE": "1", "SIGNAL_GATE_HOME": "/opt/signal-gate"})
        checked_now = datetime.now(timezone.utc) if now is None else now
        if result.returncode != 0:
            return unknown(checked_now)
        return normalize(result.stdout, checked_now)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, RecursionError):
        return unknown(datetime.now(timezone.utc) if now is None else now)


def write_snapshot(payload: dict, destination: Path = OUTPUT) -> None:
    parent = destination.parent
    for component in reversed((parent.absolute(), *parent.absolute().parents)):
        if not stat.S_ISDIR(component.lstat().st_mode):
            raise ValueError("unsafe directory")
    info = parent.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("private directory required")
    raw = (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("oversized snapshot")
    descriptor, name = tempfile.mkstemp(prefix=".inputs-", dir=parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, destination)
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if os.path.exists(name):
            os.unlink(name)


def main() -> int:
    try:
        account = pwd.getpwnam("signal-gate")
        if len(sys.argv) != 1 or (os.geteuid(), os.getegid()) != (account.pw_uid, account.pw_gid):
            raise ValueError("wrong execution identity")
        payload = collect()
        write_snapshot(payload)
    except (OSError, ValueError, KeyError):
        print("input_status state=unknown")
        return 1
    print("input_status state=" + payload["state"] + " issues=" + str(len(payload["quality_issues"])))
    return 1 if payload["state"] == "unknown" else 0


if __name__ == "__main__":
    raise SystemExit(main())
