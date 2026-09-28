"""Durable, append-only nng8 research inputs and byte-bound receipts.

Only this canonical store is handled here. Legacy inbox files are never read,
converted, or upgraded. Authentication and HTTP date windows belong to callers.
"""
from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

_contract_spec = importlib.util.spec_from_file_location("receiver_research_contract", Path(__file__).with_name("research_contract.py"))
research_contract = importlib.util.module_from_spec(_contract_spec)
_contract_spec.loader.exec_module(research_contract)

MAX_RAW_BYTES = 512 * 1024
MAX_RECORD_BYTES = 2 * MAX_RAW_BYTES
STORE_DIRECTORY = "research-receipts"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z", re.ASCII)


class InvalidRecord(ValueError):
    """Invalid input or corrupt/unsafe persisted evidence; message is public-safe."""


class ContentConflict(ValueError):
    """A report day already has a different immutable body."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidRecord("duplicate_json_key")
        result[key] = value
    return result


def _nonfinite(_value):
    raise InvalidRecord("non_finite_json")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise InvalidRecord("non_finite_json")
    return number


def _json(raw):
    try:
        # The business consumer reads UTF-8, so do not accept json.loads' extra
        # UTF-16/32 autodetection here and acknowledge an unreadable input.
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        value = json.loads(text, object_pairs_hook=_object, parse_constant=_nonfinite,
                           parse_float=_finite_float)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise InvalidRecord("invalid_json") from None
    if not isinstance(value, dict):
        raise InvalidRecord("json_not_object")
    return value


def _day(value):
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise InvalidRecord("invalid_date")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise InvalidRecord("invalid_date") from None
    return value


def _identity(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_RAW_BYTES:
        raise InvalidRecord("invalid_body_size_or_type")
    body = _json(raw)
    if body.get("source") != "nng8":
        raise InvalidRecord("invalid_source")
    target = _day(body.get("target_date"))
    as_of = _day(body.get("as_of"))
    if as_of > target:
        raise InvalidRecord("date_order_mismatch")
    if body.get("signal_type") == "research_skip":
        skip = body.get("research_skip")
        if not isinstance(skip, dict) or skip.get("target_date") != target or as_of != target:
            raise InvalidRecord("invalid_skip_identity")
        encoded = json.dumps(skip, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return {"source": "nng8", "target_date": target, "as_of": as_of,
                "payload_kind": "skip", "skip_sha256": hashlib.sha256(encoded).hexdigest()}
    contract = body.get("research_contract")
    if not isinstance(contract, dict):
        raise InvalidRecord("missing_research_contract")
    if contract.get("report_date") != target or contract.get("as_of") != as_of:
        raise InvalidRecord("contract_date_mismatch")
    if "target_date" in contract and contract["target_date"] != target:
        raise InvalidRecord("contract_date_mismatch")
    if "source" in contract and contract["source"] != "nng8":
        raise InvalidRecord("contract_source_mismatch")
    report_hash = contract.get("report_sha256")
    if not isinstance(report_hash, str) or not _HEX.fullmatch(report_hash):
        raise InvalidRecord("invalid_report_hash")
    if "report_sha256" in body and body["report_sha256"] != report_hash:
        raise InvalidRecord("contract_report_hash_mismatch")
    return {"source": "nng8", "target_date": target, "as_of": as_of,
            "report_sha256": report_hash}


def _private(fd, *, directory=False):
    info = os.fstat(fd)
    expected = 0o700 if directory else 0o600
    if (stat.S_IMODE(info.st_mode) != expected or info.st_uid != os.geteuid()
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise InvalidRecord("unsafe_record_permissions_or_type")


def _base_fd(base):
    """Open every ancestor without following symlinks; keep descriptor anchoring."""
    path = Path(base)
    if not path.is_absolute() or ".." in path.parts:
        raise InvalidRecord("unsafe_base_path")
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as exc:
        os.close(fd)
        if isinstance(exc, FileNotFoundError):
            raise FileNotFoundError("record_not_found") from None
        raise InvalidRecord("unsafe_base_path") from None


def _store_fd(base, *, create=False):
    fd = _base_fd(base)
    try:
        for part in ("inbox", "nng8", STORE_DIRECTORY):
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(fd)
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                raise FileNotFoundError("record_not_found") from None
            except OSError:
                raise InvalidRecord("unsafe_record_directory") from None
            os.close(fd)
            fd = child
            if part == STORE_DIRECTORY:
                _private(fd, directory=True)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _ack(record):
    result = {"status": "accepted", "source": record["source"], "day": record["as_of"],
            "as_of": record["as_of"], "target_date": record["target_date"], "stored": True,
            "content_sha256": record["content_sha256"], "record_id": record["record_id"],
            "stored_at": record["stored_at"]}
    if record.get("payload_kind") == "skip":
        result.update(payload_kind="skip", skip_sha256=record["skip_sha256"])
    return result


def _decode_record(encoded, target):
    record = _json(encoded)
    if record.get("version") != 2 or type(record.get("version")) is not int:
        raise InvalidRecord("unsupported_record_version")
    try:
        raw = base64.b64decode(record["raw_body_base64"], validate=True)
    except (KeyError, ValueError, TypeError, binascii.Error):
        raise InvalidRecord("invalid_record_body") from None
    identity = _identity(raw)
    if identity["target_date"] != target or any(record.get(k) != v for k, v in identity.items()):
        raise InvalidRecord("stored_identity_mismatch")
    if (record.get("content_sha256") != hashlib.sha256(raw).hexdigest()
            or type(record.get("payload_bytes")) is not int or record["payload_bytes"] != len(raw)):
        raise InvalidRecord("stored_content_mismatch")
    identifier = record.get("record_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"rr_[0-9a-f]{32}", identifier):
        raise InvalidRecord("invalid_record_id")
    try:
        recorded_at = datetime.fromisoformat(record["stored_at"])
        if recorded_at.tzinfo is None or recorded_at.utcoffset().total_seconds() != 0:
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise InvalidRecord("invalid_stored_at") from None
    return raw, _ack(record)


def _read_at(fd, target):
    try:
        record_fd = os.open(target + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        raise FileNotFoundError("record_not_found") from None
    except OSError:
        raise InvalidRecord("unsafe_record_file") from None
    try:
        _private(record_fd)
        with os.fdopen(record_fd, "rb", closefd=False) as handle:
            encoded = handle.read(MAX_RECORD_BYTES + 1)
        if len(encoded) > MAX_RECORD_BYTES:
            raise InvalidRecord("record_too_large")
        return _decode_record(encoded, target)
    finally:
        os.close(record_fd)


def read_record(base, source, target_date):
    """Read and rehash persisted original bytes; no mkdir, lock, or file writes."""
    if source != "nng8":
        raise InvalidRecord("invalid_source")
    target = _day(target_date)
    fd = _store_fd(base)
    try:
        return _read_at(fd, target)
    finally:
        os.close(fd)


def capture(base, raw, *, now=None):
    """Atomically create a canonical input, or return its original durable receipt.

    A publication/fsync error can leave a complete but unacknowledged record. It
    is never reported as stored by this call and must be investigated read-only.
    """
    identity = _identity(raw)
    # Do not reserve an immutable date for a contract the renderer will reject.
    # Historical reads remain byte/identity validation only: never rewrite old
    # accepted records or pretend a tightened policy was in force in the past.
    try:
        research_contract.validate(_json(raw), now or datetime.now(timezone.utc))
    except (ValueError, TypeError, KeyError, OverflowError):
        raise InvalidRecord("invalid_research_contract") from None
    target = identity["target_date"]
    fd = _store_fd(base, create=True)
    lock_fd = None
    temp_name = None
    try:
        try:
            lock_fd = os.open(".capture.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                              0o600, dir_fd=fd)
        except OSError:
            raise InvalidRecord("unsafe_capture_lock") from None
        _private(lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            existing_raw, existing_ack = _read_at(fd, target)
        except FileNotFoundError:
            pass
        else:
            if existing_raw != raw:
                raise ContentConflict("content_conflict")
            return existing_ack

        record = dict(identity, version=2, raw_body_base64=base64.b64encode(raw).decode("ascii"),
                      content_sha256=hashlib.sha256(raw).hexdigest(), payload_bytes=len(raw),
                      record_id="rr_" + uuid.uuid4().hex,
                      stored_at=datetime.now(timezone.utc).isoformat())
        encoded = (json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
        # Validate generated metadata too: an invalid clock/identifier must not
        # publish a record that our own consumer would subsequently reject.
        _decode_record(encoded, target)
        temp_name = ".record-" + uuid.uuid4().hex
        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=fd)
        try:
            _private(temp_fd)
            remaining = memoryview(encoded)
            while remaining:
                size = os.write(temp_fd, remaining)
                if size <= 0:
                    raise OSError("record_write_failed")
                remaining = remaining[size:]
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        # Atomic create-if-absent, not os.replace: even a non-cooperating writer
        # cannot make this operation overwrite an existing canonical record.
        os.link(temp_name, target + ".json", src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.unlink(temp_name, dir_fd=fd)
        temp_name = None
        os.fsync(fd)
        stored_raw, ack = _read_at(fd, target)
        if stored_raw != raw:
            raise InvalidRecord("stored_content_mismatch")
        return ack
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=fd)
            except FileNotFoundError:
                pass
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(fd)
