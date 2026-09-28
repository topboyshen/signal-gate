import base64
import concurrent.futures
import hashlib
import json
import multiprocessing
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from functools import partial
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import research_receipts as receipts
from test_sealed_research import payload


DAY = "2026-09-21"
AS_OF = "2026-09-18"
NOW = datetime.fromisoformat("2026-09-21T08:15:00+08:00")


def body(**changes):
    data = payload(NOW)
    data["summary"] = "隔离样本，不发送"
    data.update(changes)
    return json.dumps(data, ensure_ascii=False, indent=2).encode()


def capture_process(args):
    base, raw = args
    try:
        return "ok", receipts.capture(base, raw, now=NOW)
    except receipts.ContentConflict:
        return "conflict", None


class ReceiptStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS /var is a symlink; production APIs intentionally require real paths.
        self.base = Path(self.temp.name).resolve()
        self.raw = body()
        self.directory = self.base / "inbox" / "nng8" / receipts.STORE_DIRECTORY
        self.path = self.directory / (DAY + ".json")
        capture = patch.object(receipts, "capture", partial(receipts.capture, now=NOW))
        capture.start()
        self.addCleanup(capture.stop)

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        return receipts.capture(self.base, self.raw)

    def test_original_bytes_receipt_and_private_modes(self):
        ack = self.save()
        raw, reread = receipts.read_record(self.base, "nng8", DAY)
        self.assertEqual(raw, self.raw)
        self.assertEqual(ack, reread)
        self.assertEqual(ack["day"], AS_OF)
        self.assertEqual(ack["target_date"], DAY)
        self.assertEqual(ack["content_sha256"], hashlib.sha256(self.raw).hexdigest())
        normalized = json.dumps(json.loads(self.raw), separators=(",", ":")).encode()
        self.assertNotEqual(ack["content_sha256"], hashlib.sha256(normalized).hexdigest())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertNotIn("raw_body_base64", ack)
        self.assertNotIn(str(self.base), json.dumps(ack))

    def test_duplicate_returns_original_receipt_without_changing_record(self):
        first = self.save()
        data, mtime = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.assertEqual(receipts.capture(self.base, self.raw), first)
        self.assertEqual(self.path.read_bytes(), data)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)

    def test_conflict_keeps_original(self):
        first = self.save()
        original = self.path.read_bytes()
        with self.assertRaises(receipts.ContentConflict):
            receipts.capture(self.base, body(summary="不同内容"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(receipts.read_record(self.base, "nng8", DAY)[1], first)

    def test_restart_process_can_read_original_receipt(self):
        first = self.save()
        with concurrent.futures.ProcessPoolExecutor(max_workers=1,
                mp_context=multiprocessing.get_context("spawn")) as pool:
            result = pool.submit(capture_process, (str(self.base), self.raw)).result(timeout=15)
        self.assertEqual(result, ("ok", first))

    def test_parallel_processes_same_body_have_one_record(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=4,
                mp_context=multiprocessing.get_context("spawn")) as pool:
            results = list(pool.map(capture_process, [(str(self.base), self.raw)] * 8))
        self.assertTrue(all(status == "ok" for status, _ in results))
        self.assertEqual(len({ack["record_id"] for _, ack in results}), 1)
        self.assertEqual(len(list(self.directory.glob("*.json"))), 1)
        self.assertFalse(list(self.directory.glob(".record-*")))

    def test_parallel_conflicting_processes_only_one_wins(self):
        requests = [(str(self.base), body(summary="variation-" + str(i))) for i in range(8)]
        with concurrent.futures.ProcessPoolExecutor(max_workers=4,
                mp_context=multiprocessing.get_context("spawn")) as pool:
            results = list(pool.map(capture_process, requests))
        self.assertEqual(sum(status == "ok" for status, _ in results), 1)
        self.assertEqual(sum(status == "conflict" for status, _ in results), 7)
        self.assertEqual(len(list(self.directory.glob("*.json"))), 1)

    def test_get_absent_never_creates_store(self):
        with self.assertRaises(FileNotFoundError):
            receipts.read_record(self.base, "nng8", DAY)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_get_has_no_writes_or_locks(self):
        self.save()
        before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.directory.iterdir()}
        with patch.object(receipts.os, "mkdir", side_effect=AssertionError("write")), \
             patch.object(receipts.os, "write", side_effect=AssertionError("write")), \
             patch.object(receipts.fcntl, "flock", side_effect=AssertionError("lock")):
            receipts.read_record(self.base, "nng8", DAY)
        after = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.directory.iterdir()}
        self.assertEqual(before, after)

    def test_legacy_inbox_is_never_read_or_upgraded(self):
        legacy = self.base / "inbox" / "nng8"
        legacy.mkdir(parents=True)
        old = legacy / ("digest-" + AS_OF + ".json")
        old.write_bytes(self.raw)
        with self.assertRaises(FileNotFoundError):
            receipts.read_record(self.base, "nng8", DAY)
        self.assertEqual(old.read_bytes(), self.raw)
        self.assertFalse(self.directory.exists())

    def test_invalid_identity_is_rejected_before_writes(self):
        cases = [body(source="other"), body(target_date="2026-02-30"), body(as_of="2026-09-22"),
                 body(target_date="../../secret"), body(target_date=None), body(research_contract={}),
                 body(research_contract={"report_date": "2026-09-20", "as_of": AS_OF, "report_sha256": "a" * 64}),
                 body(research_contract={"report_date": DAY, "target_date": "2026-09-20", "as_of": AS_OF, "report_sha256": "a" * 64}),
                 body(research_contract={"report_date": DAY, "as_of": AS_OF, "report_sha256": "wrong"}),
                 body(report_sha256="b" * 64), b'{"source":"nng8","source":"other"}', b"NaN", b"[]", b"", b"\xff"]
        for raw in cases:
            with self.subTest(raw=raw[:60]):
                with self.assertRaises(receipts.InvalidRecord):
                    receipts.capture(self.base, raw)
                self.assertEqual(list(self.base.iterdir()), [])

    def test_ready_contract_equal_dates_fail_closed_like_renderer(self):
        raw = body(as_of=DAY, research_contract={"report_date": DAY, "as_of": DAY, "report_sha256": "a" * 64})
        with self.assertRaises(receipts.InvalidRecord):
            receipts.capture(self.base, raw)
        self.assertFalse(self.directory.exists())

    def test_optional_contract_target_must_match(self):
        contract = payload(NOW)["research_contract"]
        contract["target_date"] = DAY
        raw = body(research_contract=contract)
        self.assertEqual(receipts.capture(self.base, raw)["target_date"], DAY)

    def test_body_larger_than_consumer_limit_rejected_before_write(self):
        with self.assertRaises(receipts.InvalidRecord):
            receipts.capture(self.base, body(summary="x" * receipts.MAX_RAW_BYTES))
        self.assertEqual(list(self.base.iterdir()), [])

    def test_non_utf8_and_numeric_overflow_are_not_acknowledged(self):
        for raw in (self.raw.decode().encode("utf-16"), self.raw.replace(b'"summary":', b'"extra": 1e999, "summary":')):
            with self.assertRaises(receipts.InvalidRecord):
                receipts.capture(self.base, raw)
            self.assertEqual(list(self.base.iterdir()), [])

    def test_naive_generated_clock_is_rejected_before_record_publication(self):
        class BadClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 21, 0, 0, 0)
        with patch.object(receipts, "datetime", BadClock):
            with self.assertRaises(receipts.InvalidRecord):
                self.save()
        self.assertFalse(self.path.exists())

    def test_post_store_readback_failure_cannot_acknowledge(self):
        real_read = receipts._read_at
        calls = 0
        def corrupt_readback(fd, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise receipts.InvalidRecord("synthetic-readback-failure")
            return real_read(fd, target)
        with patch.object(receipts, "_read_at", side_effect=corrupt_readback):
            with self.assertRaises(receipts.InvalidRecord):
                self.save()
        self.assertEqual(receipts.read_record(self.base, "nng8", DAY)[0], self.raw)

    def test_get_invalid_source_or_date(self):
        for source, day in [("other", DAY), ("../nng8", DAY), ("nng8", "2026-02-30")]:
            with self.assertRaises(receipts.InvalidRecord):
                receipts.read_record(self.base, source, day)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_tampered_body_hash_and_metadata_rejected(self):
        self.save()
        original = json.loads(self.path.read_bytes())
        variants = [{"content_sha256": "b" * 64}, {"raw_body_base64": base64.b64encode(body(summary="tamper")).decode()},
                    {"payload_bytes": True}, {"target_date": "2026-09-20"}, {"record_id": ""},
                    {"stored_at": "2026-09-20T00:00:00"}, {"version": True}, {"raw_body_base64": "!"}]
        for variation in variants:
            with self.subTest(fields=list(variation)):
                self.path.write_text(json.dumps(dict(original, **variation)))
                with self.assertRaises(receipts.InvalidRecord):
                    receipts.read_record(self.base, "nng8", DAY)
                with self.assertRaises(receipts.InvalidRecord):
                    receipts.capture(self.base, self.raw)

    def test_truncated_record_rejected(self):
        self.save()
        self.path.write_bytes(b'{"version":2,')
        with self.assertRaises(receipts.InvalidRecord):
            receipts.read_record(self.base, "nng8", DAY)

    def test_symlinked_base_rejected(self):
        link = self.base / "alias"
        link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(receipts.InvalidRecord):
            receipts.capture(link, self.raw)

    def test_symlinked_record_directory_rejected(self):
        outside = self.base / "other"
        outside.mkdir(mode=0o700)
        self.directory.parent.mkdir(parents=True)
        self.directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(receipts.InvalidRecord):
            self.save()
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_record_and_lock_rejected(self):
        self.save()
        self.path.unlink()
        target = self.base / "do-not-touch"
        target.write_bytes(b"keep")
        self.path.symlink_to(target)
        with self.assertRaises(receipts.InvalidRecord):
            receipts.read_record(self.base, "nng8", DAY)
        with self.assertRaises(receipts.InvalidRecord):
            self.save()
        self.path.unlink()
        lock = self.directory / ".capture.lock"
        lock.unlink()
        lock.symlink_to(target)
        with self.assertRaises(receipts.InvalidRecord):
            self.save()
        self.assertEqual(target.read_bytes(), b"keep")

    def test_nonprivate_record_or_directory_rejected(self):
        self.save()
        self.path.chmod(0o644)
        with self.assertRaises(receipts.InvalidRecord):
            receipts.read_record(self.base, "nng8", DAY)
        self.path.chmod(0o600)
        self.directory.chmod(0o755)
        with self.assertRaises(receipts.InvalidRecord):
            receipts.read_record(self.base, "nng8", DAY)

    def test_write_failure_never_acknowledges_or_leaves_partial_record(self):
        # Pre-create directories so injection targets the actual file write.
        self.directory.mkdir(parents=True, mode=0o700)
        self.directory.parent.chmod(0o700)
        with patch.object(receipts.os, "write", side_effect=OSError("synthetic-write-failure")):
            with self.assertRaises(OSError):
                self.save()
        self.assertFalse(self.path.exists())
        self.assertFalse(list(self.directory.glob(".record-*")))

    def test_file_fsync_failure_never_publishes(self):
        self.directory.mkdir(parents=True, mode=0o700)
        self.directory.parent.chmod(0o700)
        with patch.object(receipts.os, "fsync", side_effect=OSError("synthetic-fsync-failure")):
            with self.assertRaises(OSError):
                self.save()
        self.assertFalse(self.path.exists())
        self.assertFalse(list(self.directory.glob(".record-*")))

    def test_atomic_publication_is_create_not_overwrite(self):
        real_link = receipts.os.link
        def competing_writer(src, dst, **kwargs):
            fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=kwargs["dst_dir_fd"])
            os.write(fd, b"competing-record")
            os.close(fd)
            return real_link(src, dst, **kwargs)
        with patch.object(receipts.os, "link", side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                self.save()
        self.assertEqual(self.path.read_bytes(), b"competing-record")
        self.assertFalse(list(self.directory.glob(".record-*")))

    def test_directory_fsync_failure_never_returns_success(self):
        self.directory.mkdir(parents=True, mode=0o700)
        self.directory.parent.chmod(0o700)
        real_fsync = receipts.os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("synthetic-directory-fsync-failure")
            return real_fsync(fd)
        with patch.object(receipts.os, "fsync", side_effect=fail_directory):
            with self.assertRaises(OSError):
                self.save()
        # Publication may have happened; no success was returned. Any retained
        # record is complete and can only be investigated using read-only GET.
        self.assertEqual(receipts.read_record(self.base, "nng8", DAY)[0], self.raw)


if __name__ == "__main__":
    unittest.main()
