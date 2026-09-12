#!/usr/bin/env python3
"""Local JD reception evidence and read-only, news-only point-in-time replay.

No inferred legacy timestamps, credentials, network, database or delivery IO.
Receipt integrity assumes the service account/filesystem and host clock are
trusted; a digest hash is not a signature against a compromised local account.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
import uuid

UTC = timezone.utc
MAX_BODY_BYTES = 1_000_000
MAX_RECORD_BYTES = 2 * MAX_BODY_BYTES + 4096
MAX_RECORDS = 10000
RECORD_NAME = re.compile(r"([0-9]{8}T[0-9]{6}\.[0-9]{6}Z)-[0-9a-f]{32}\.json")
WRITE_LOCK = Lock()


class HistoryError(ValueError):
    """Only fixed safe reason codes are exposed to logs/CLI."""


def precise_time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise HistoryError("time_unknown")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        raise HistoryError("time_invalid") from None


def _confined(base: Path, path: Path) -> None:
    try:
        if not path.resolve().is_relative_to(base.resolve()):
            raise HistoryError("path_invalid")
        for component in (path, *path.parents):
            if component == base:
                break
            if component.is_symlink():
                raise HistoryError("path_invalid")
    except (OSError, RuntimeError):
        raise HistoryError("path_invalid") from None


def _json_file(base: Path, path: Path, limit: int) -> dict:
    _confined(base, path)
    try:
        if not path.is_file():
            raise HistoryError("receipt_missing" if not path.exists() else "receipt_invalid")
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise HistoryError("input_too_large")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise HistoryError("receipt_invalid")
        return data
    except FileNotFoundError:
        raise HistoryError("receipt_missing") from None
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise HistoryError("receipt_invalid") from None


def atomic_write(path: Path, raw: bytes, *, exclusive: bool = False) -> None:
    """Publish a complete 0600 file; reception records are never overwritten."""
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)  # atomic no-clobber publication
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def utc_now() -> datetime:
    return datetime.now(UTC)


def capture(base: Path, raw_body: bytes, day: str) -> dict:
    """Called only after JD HMAC and JSON-object validation; ignore sender clocks."""
    if len(raw_body) > MAX_BODY_BYTES:
        raise HistoryError("input_too_large")
    # Defence in depth: filenames cannot come from arbitrary sender strings.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise HistoryError("day_invalid")
    with WRITE_LOCK:
        folder = base / "inbox" / "jd"
        history = folder / "received"
        _confined(base, history)
        history.mkdir(mode=0o700, parents=True, exist_ok=True)
        now = utc_now()
        if now.tzinfo is None:
            raise HistoryError("clock_invalid")
        now = now.astimezone(UTC)
        name = now.strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex + ".json"
        record = {
            "schema_version": 1, "source": "jd", "received_at": now.isoformat(),
            "body_sha256": hashlib.sha256(raw_body).hexdigest(),
            "body_utf8": raw_body.decode("utf-8"),
        }
        encoded = json.dumps(record, ensure_ascii=False).encode("utf-8")
        atomic_write(history / name, encoded, exclusive=True)
        # Preserve the exact authenticated bytes. Readers verify the pointer
        # against both the immutable record and the latest payload; races fail
        # closed instead of attaching an old receipt to new content.
        atomic_write(folder / f"digest-{day}.json", raw_body)
        atomic_write(folder / "digest-latest.json", raw_body)
        atomic_write(folder / "digest-latest.receipt.json", json.dumps({"record": name}).encode())
        return {key: value for key, value in record.items() if key != "body_utf8"}


def read_record(base: Path, name: str) -> tuple[str, dict]:
    match = RECORD_NAME.fullmatch(name)
    if match is None:
        raise HistoryError("receipt_invalid")
    record = _json_file(base, base / "inbox" / "jd" / "received" / name, MAX_RECORD_BYTES)
    if type(record.get("schema_version")) is not int or record["schema_version"] != 1 or record.get("source") != "jd":
        raise HistoryError("receipt_invalid")
    received = precise_time(record.get("received_at"))
    if received.strftime("%Y%m%dT%H%M%S.%fZ") != match[1]:
        raise HistoryError("receipt_invalid")
    body = record.get("body_utf8")
    if not isinstance(body, str):
        raise HistoryError("receipt_invalid")
    try:
        raw = body.encode("utf-8")
    except UnicodeError:
        raise HistoryError("receipt_invalid") from None
    if len(raw) > MAX_BODY_BYTES or hashlib.sha256(raw).hexdigest() != record.get("body_sha256"):
        raise HistoryError("body_mismatch")
    try:
        if not isinstance(json.loads(body), dict):
            raise HistoryError("receipt_invalid")
    except (ValueError, RecursionError):
        raise HistoryError("receipt_invalid") from None
    return body, {"received_at": received.isoformat(), "body_sha256": record["body_sha256"]}


def latest_receipt(base: Path, body: str) -> dict:
    pointer = _json_file(base, base / "inbox" / "jd" / "digest-latest.receipt.json", 512)
    name = pointer.get("record")
    if not isinstance(name, str):
        raise HistoryError("receipt_invalid")
    recorded_body, metadata = read_record(base, name)
    if recorded_body != body:
        raise HistoryError("body_mismatch")
    return metadata


def select_before(base: Path, cutoff: datetime) -> tuple[str, dict]:
    """Select the latest receiver-observed snapshot at/before an aware cutoff.

    Never use mtime, sender received_at, digest-latest or dated legacy aliases.
    Never fall back past a corrupt/ambiguous selected record to look healthy.
    """
    if cutoff.tzinfo is None:
        raise HistoryError("cutoff_invalid")
    folder = base / "inbox" / "jd" / "received"
    _confined(base, folder)
    stamp = cutoff.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    names = []
    try:
        for count, entry in enumerate(folder.iterdir(), start=1):
            if count > MAX_RECORDS:
                raise HistoryError("history_limit")
            if entry.name.startswith(".pending-"):
                continue
            match = RECORD_NAME.fullmatch(entry.name)
            if match is None:
                raise HistoryError("receipt_invalid")
            if match[1] <= stamp:
                names.append(entry.name)
    except FileNotFoundError:
        raise HistoryError("history_missing") from None
    except OSError:
        raise HistoryError("history_unavailable") from None
    if not names:
        raise HistoryError("no_record_before_cutoff")
    names.sort(reverse=True)
    if len(names) > 1 and names[0].split("-")[0] == names[1].split("-")[0]:
        raise HistoryError("ambiguous_reception")
    body, metadata = read_record(base, names[0])
    if precise_time(metadata["received_at"]) > cutoff:
        raise HistoryError("receipt_invalid")
    return body, metadata


def replay(base: Path, cutoff: datetime) -> dict:
    snapshot = select_before(base, cutoff)
    # Explicit sibling loading avoids consulting any deployment state/import path.
    spec = importlib.util.spec_from_file_location("news_replay_content", Path(__file__).with_name("digest_content.py"))
    assert spec is not None and spec.loader is not None
    content = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(content)
    renderer = content.Renderer(base, base / "unused.db", cutoff.astimezone(content.TZ), news_snapshot=snapshot)
    lines = renderer.highlights("jd")  # Deliberately NOT report()/reviews()/execution().
    return {"mode": "news_only_as_of", "cutoff": cutoff.isoformat(),
            "status": "degraded" if renderer.issues else "ok", "issues": renderer.issues,
            **snapshot[1], "text": "\n".join(lines), "source": renderer.sources["jd"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only JD history; no database or Telegram access")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--cutoff", required=True, help="ISO timestamp with explicit timezone")
    parser.add_argument("--text", action="store_true", help="Show sanitized news rather than metadata only")
    args = parser.parse_args(argv)
    try:
        result = replay(args.base.resolve(), precise_time(args.cutoff))
    except HistoryError as exc:
        print(json.dumps({"mode": "news_only_as_of", "status": "unavailable", "reason": str(exc)}))
        return 2
    if args.text:
        print("历史资讯回放（不含人审或执行状态）")
        print(f"截止 {result['cutoff']}；本机接收 {result['received_at']}；正文 {result['body_sha256'][:12]}")
        print(result["text"])
    else:
        print(json.dumps({key: value for key, value in result.items() if key != "text"}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
