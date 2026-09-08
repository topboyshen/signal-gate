#!/usr/bin/env python3
"""Daily digest delivery with private, crash-safe receipts and offline preview."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from datetime import datetime
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

try:
    from digest_content import build_report
except ModuleNotFoundError as exc:
    if exc.name != "digest_content":
        raise
    # spec-based offline loaders do not add app/ to sys.path. Load the explicit
    # sibling instead of changing global import paths or consulting production.
    import importlib.util
    _content_spec = importlib.util.spec_from_file_location(
        "signal_gate_digest_content", Path(__file__).with_name("digest_content.py")
    )
    _content = importlib.util.module_from_spec(_content_spec)
    _content_spec.loader.exec_module(_content)
    build_report = _content.build_report

BASE = Path(os.environ.get("SIGNAL_GATE_HOME", "/opt/signal-gate"))
DB_PATH = Path(os.environ.get("SIGNAL_GATE_DB", str(BASE / "data" / "signals.db")))
SECRETS_PATH = Path(os.environ.get("SIGNAL_GATE_SECRETS", str(BASE / "secrets.env")))
OUT = BASE / "out"
TZ = ZoneInfo("Asia/Shanghai")
MAX_PART_CHARS = 3500
MAX_PARTS = 64
DELIVERY_BUDGET_SECONDS = 90
REQUEST_TIMEOUT_SECONDS = 15
MAX_ACK_BYTES = 16384
SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SAFE_ISSUE = re.compile(
    r"^(nng8|jd|quant|review):(missing|stale|unknown_date|future|invalid|schema_error|"
    r"too_large|truncated|io_error|expiry_unknown|placeholder)$"
)
HEX_HASH = re.compile(r"^[0-9a-f]{64}$")
STATUSES = {"confirmed", "failed", "unknown", "pending", "blocked"}
log = logging.getLogger("signal-gate-digest")


class DeliveryError(Exception):
    def __init__(self, code: str, status: str = "failed"):
        self.code = code if SAFE_CODE.fullmatch(code) else "delivery_error"
        self.status = status if status in {"failed", "unknown", "blocked"} else "unknown"
        super().__init__(self.code)


def load_secrets(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def split_text(text: str, limit: int = MAX_PART_CHARS) -> list[str]:
    """Preserve every character, preferring line boundaries to hard splits."""
    if not isinstance(text, str) or not text.strip() or limit < 1:
        raise DeliveryError("report_invalid")
    parts = []
    remaining = text
    while len(remaining) > limit:
        boundary = remaining.rfind("\n", 0, limit)
        cut = boundary + 1 if boundary >= limit // 2 else limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        parts.append(remaining)
    if len(parts) > MAX_PARTS:
        raise DeliveryError("report_too_large")
    return parts


def code_version() -> str:
    digest = hashlib.sha256()
    for name in ("digest.py", "digest_content.py"):
        digest.update(name.encode() + b"\0")
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def _timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def _safe_issues(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 64 or any(
        not isinstance(item, str) or len(item) > 64 or not SAFE_ISSUE.fullmatch(item) for item in value
    ):
        raise DeliveryError("report_invalid")
    return sorted(set(value))


def _validated_report(value: object, day: str) -> dict:
    if not isinstance(value, dict) or value.get("report_date") != day:
        raise DeliveryError("report_invalid")
    text = value.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
        raise DeliveryError("report_invalid")
    issues = _safe_issues(value.get("issues"))
    if not isinstance(value.get("degraded"), bool) or value["degraded"] != bool(issues):
        raise DeliveryError("report_invalid")
    return {"report_date": day, "text": text,
            "issues": issues, "degraded": value["degraded"]}


def _private_directory(path: Path, *, create: bool = True) -> None:
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise DeliveryError("state_unavailable", "blocked")


def _atomic_write(path: Path, data: bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise DeliveryError("state_unavailable", "blocked")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_receipt(path: Path, receipt: dict) -> None:
    receipt["updated_at"] = datetime.now(TZ).isoformat()
    _atomic_write(path, (json.dumps(receipt, ensure_ascii=True, sort_keys=True) + "\n").encode())


def read_receipt(path: Path, day: str) -> dict | None:
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or (info.st_uid, info.st_gid) != (os.geteuid(), os.getegid()) or info.st_size > 65536):
        raise DeliveryError("receipt_invalid", "blocked")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        keys = {"schema_version", "report_date", "status", "updated_at", "generated_at",
                "content_sha256", "code_version", "issues", "parts", "last_error_code"}
        if not isinstance(receipt, dict) or set(receipt) not in (keys, keys | {"confirmed_at"}):
            raise ValueError
        if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
            raise ValueError
        if receipt["report_date"] != day or receipt["status"] not in STATUSES:
            raise ValueError
        if not all(_timestamp(receipt[key]) for key in ("updated_at", "generated_at")):
            raise ValueError
        if not all(isinstance(receipt[key], str) and HEX_HASH.fullmatch(receipt[key])
                   for key in ("content_sha256", "code_version")):
            raise ValueError
        _safe_issues(receipt["issues"])
        if not isinstance(receipt["last_error_code"], str) or not SAFE_CODE.fullmatch(receipt["last_error_code"]):
            raise ValueError
        parts = receipt["parts"]
        if not isinstance(parts, list) or len(parts) > MAX_PARTS:
            raise ValueError
        confirmed_ids = set()
        for index, part in enumerate(parts, 1):
            if not isinstance(part, dict) or set(part) != {"index", "status", "message_id"}:
                raise ValueError
            if type(part["index"]) is not int or part["index"] != index:
                raise ValueError
            if part["status"] not in {"confirmed", "failed", "unknown", "pending"}:
                raise ValueError
            message_id = part["message_id"]
            if part["status"] == "confirmed":
                if type(message_id) is not int or message_id <= 0 or message_id in confirmed_ids:
                    raise ValueError
                confirmed_ids.add(message_id)
            elif message_id is not None:
                raise ValueError
        if receipt["status"] == "confirmed" and (
            not parts or any(part["status"] != "confirmed" for part in parts)
            or not _timestamp(receipt.get("confirmed_at"))
        ):
            raise ValueError
        return receipt
    except (ValueError, TypeError, KeyError, DeliveryError, OSError):
        raise DeliveryError("receipt_invalid", "blocked") from None


def _decode_ack(raw: bytes) -> dict:
    if len(raw) > MAX_ACK_BYTES:
        raise DeliveryError("ack_invalid", "unknown")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise DeliveryError("ack_invalid", "unknown") from None
    if not isinstance(value, dict):
        raise DeliveryError("ack_invalid", "unknown")
    return value


def send_telegram(token: str, chat_id: str, text: str, *, deadline: float | None = None) -> int:
    """Return a confirmed message id; retry only explicit rejected 429 responses."""
    deadline = time.monotonic() + DELIVERY_BUDGET_SECONDS if deadline is None else deadline
    payload = json.dumps({"chat_id": chat_id, "text": text,
                          "disable_web_page_preview": True}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeliveryError("delivery_budget_exceeded")
        status = 0
        try:
            with urllib.request.urlopen(request, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining)) as response:
                status = response.status
                raw = response.read(MAX_ACK_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(MAX_ACK_BYTES + 1)
            except Exception:
                raw = b""
        except (TimeoutError, urllib.error.URLError):
            raise DeliveryError("network_unknown", "unknown") from None
        except Exception:
            raise DeliveryError("transport_unknown", "unknown") from None
        if type(status) is not int:
            raise DeliveryError("ack_invalid", "unknown")
        if status >= 500 or status == 408:
            raise DeliveryError("upstream_unknown", "unknown")
        try:
            ack = _decode_ack(raw)
        except DeliveryError:
            if 400 <= status < 500:
                raise DeliveryError("rate_limited" if status == 429 else "request_rejected") from None
            raise
        rejected = ack.get("ok") is False
        error_code = ack.get("error_code")
        rate_limited = status == 429 or (rejected and type(error_code) is int and error_code == 429)
        if rate_limited:
            parameters = ack.get("parameters")
            delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if (not rejected or type(delay) is not int or not 0 <= delay <= 10
                    or attempt >= 2 or delay + 1 >= deadline - time.monotonic()):
                raise DeliveryError("rate_limited")
            time.sleep(delay)
            continue
        if 400 <= status < 500:
            raise DeliveryError("auth_rejected" if status in (401, 403) else "request_rejected")
        if rejected and type(error_code) is int and 400 <= error_code < 500:
            raise DeliveryError("auth_rejected" if error_code in (401, 403) else "request_rejected")
        result = ack.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not (200 <= status < 300 and ack.get("ok") is True
                and type(message_id) is int and message_id > 0):
            raise DeliveryError("ack_invalid", "unknown")
        return message_id
    raise DeliveryError("rate_limited")


def _new_receipt(day: str, now: datetime, version: str, text: str, issues: list[str]) -> dict:
    return {"schema_version": 1, "report_date": day, "status": "pending",
            "updated_at": now.isoformat(), "generated_at": now.isoformat(),
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "code_version": version, "issues": issues,
            "parts": [{"index": index, "status": "pending", "message_id": None}
                      for index, _ in enumerate(split_text(text) if text else [], 1)],
            "last_error_code": "none"}


def _event(day: str, status: str, code: str, receipt: dict | None = None) -> None:
    parts = receipt["parts"] if receipt else []
    print(f"digest_delivery report_date={day} status={status} code={code} "
          f"parts_total={len(parts)} parts_confirmed={sum(p['status'] == 'confirmed' for p in parts)} "
          f"issues={len(receipt['issues']) if receipt else 0}")


def run_digest(*, dry_run: bool = False, json_output: bool = False, now: datetime | None = None) -> int:
    now = datetime.now(TZ) if now is None else now.astimezone(TZ)
    day = now.date().isoformat()
    deadline = time.monotonic() + DELIVERY_BUDGET_SECONDS
    try:
        version = code_version()
    except Exception:
        if dry_run and json_output:
            print(json.dumps({"report_date": day, "status": "failed", "issues": ["code_unavailable"]}))
        else:
            _event(day, "failed", "code_unavailable")
        return 1
    if dry_run:
        try:
            report = _validated_report(build_report(BASE, DB_PATH, now=now), day)
            preview = {"schema_version": 1, "report_date": day, "status": "dry_run",
                       "generated_at": now.isoformat(), "issues": report["issues"],
                       "degraded": report["degraded"], "code_version": version,
                       "content_sha256": hashlib.sha256(report["text"].encode()).hexdigest(),
                       "parts_total": len(split_text(report["text"])), "chars": len(report["text"])}
            print(json.dumps(preview, sort_keys=True) if json_output else report["text"])
            return 0
        except Exception:
            if json_output:
                print(json.dumps({"report_date": day, "status": "failed", "issues": ["render_failed"]}))
            else:
                _event(day, "failed", "render_failed")
            return 1
    lock_descriptor = None
    receipt = None
    receipt_path = OUT / "delivery" / f"{day}.json"
    try:
        _private_directory(BASE, create=False)
        _private_directory(OUT)
        _private_directory(OUT / "delivery")
        directory_info = (OUT / "delivery").lstat()
        if ((directory_info.st_uid, directory_info.st_gid) != (os.geteuid(), os.getegid())
                or stat.S_IMODE(directory_info.st_mode) != 0o700):
            raise DeliveryError("state_unavailable", "blocked")
        lock_descriptor = os.open(
            OUT / "delivery" / f".{day}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        os.fchmod(lock_descriptor, 0o600)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _event(day, "blocked", "delivery_busy")
            return 1
        receipt = read_receipt(receipt_path, day)
        if receipt and receipt["status"] == "confirmed":
            _event(day, "confirmed", "already_confirmed", receipt)
            return 0
        if receipt and (receipt["status"] == "unknown" or any(p["status"] == "unknown" for p in receipt["parts"])):
            receipt.update(status="unknown", last_error_code="acceptance_unknown")
            write_receipt(receipt_path, receipt)
            _event(day, "unknown", "acceptance_unknown", receipt)
            return 1
        if receipt and receipt["parts"] and all(p["status"] == "confirmed" for p in receipt["parts"]):
            # All individual acknowledgements survived, but a crash may have
            # interrupted only the final overall receipt update.
            receipt.update(status="confirmed", last_error_code="none", confirmed_at=now.isoformat())
            write_receipt(receipt_path, receipt)
            _event(day, "confirmed", "confirmation_recovered", receipt)
            return 0
        if receipt and receipt["status"] == "blocked":
            _event(day, "blocked", receipt["last_error_code"], receipt)
            return 1
        try:
            report = _validated_report(build_report(BASE, DB_PATH, now=now), day)
        except Exception:
            receipt = receipt or _new_receipt(day, now, version, "", [])
            raise DeliveryError("render_failed") from None
        parts = split_text(report["text"])
        content_hash = hashlib.sha256(report["text"].encode()).hexdigest()
        partial = receipt and any(p["status"] == "confirmed" for p in receipt["parts"])
        if partial and (receipt["content_sha256"] != content_hash or len(receipt["parts"]) != len(parts)):
            raise DeliveryError("partial_content_changed", "blocked")
        if not partial:
            receipt = _new_receipt(day, now, version, report["text"], report["issues"])
        else:
            receipt.update(code_version=version, issues=report["issues"])
        try:
            secrets = load_secrets(SECRETS_PATH)
        except Exception:
            raise DeliveryError("config_unavailable") from None
        token = secrets.get("TELEGRAM_DIGEST_BOT_TOKEN", "").strip()
        chat_id = secrets.get("TELEGRAM_DIGEST_CHAT_ID", "").strip()
        if not token or not chat_id:
            raise DeliveryError("config_missing")
        if not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,}", token) or not re.fullmatch(r"-?[0-9]+", chat_id):
            raise DeliveryError("config_invalid")
        write_receipt(receipt_path, receipt)
        _atomic_write(OUT / f"daily-{day}.md", report["text"].encode("utf-8"))
        for index, text in enumerate(parts):
            part = receipt["parts"][index]
            if part["status"] == "confirmed":
                continue
            # Persist uncertainty before the POST. A crash cannot cause blind resend.
            receipt.update(status="pending", last_error_code="none")
            part.update(status="unknown", message_id=None)
            write_receipt(receipt_path, receipt)
            try:
                message_id = send_telegram(token, chat_id, text, deadline=deadline)
                if type(message_id) is not int or message_id <= 0:
                    raise DeliveryError("ack_invalid", "unknown")
                if any(existing["status"] == "confirmed" and existing["message_id"] == message_id
                       for existing in receipt["parts"]):
                    raise DeliveryError("ack_invalid", "unknown")
            except DeliveryError as exc:
                part["status"] = exc.status if exc.status in {"failed", "unknown"} else "failed"
                raise
            except Exception:
                raise DeliveryError("transport_unknown", "unknown") from None
            part.update(status="confirmed", message_id=message_id)
            write_receipt(receipt_path, receipt)
        receipt.update(status="confirmed", last_error_code="none", confirmed_at=datetime.now(TZ).isoformat())
        write_receipt(receipt_path, receipt)
        _event(day, "confirmed", "sent", receipt)
        return 0
    except DeliveryError as exc:
        if receipt is not None:
            receipt.update(status=exc.status, last_error_code=exc.code)
            try:
                write_receipt(receipt_path, receipt)
            except Exception:
                _event(day, "unknown", "state_write_failed")
                return 1
        _event(day, exc.status, exc.code, receipt)
        return 1
    except Exception:
        _event(day, "unknown", "state_unavailable")
        return 1
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Signal Gate daily digest")
    parser.add_argument("--dry-run", action="store_true", help="render without credentials, delivery or state writes")
    parser.add_argument("--json", action="store_true", help="safe metadata only; requires --dry-run")
    args = parser.parse_args([] if argv is None else argv)
    if args.json and not args.dry_run:
        parser.error("--json requires --dry-run")
    return run_digest(dry_run=args.dry_run, json_output=args.json)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
