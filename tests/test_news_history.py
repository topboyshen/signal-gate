"""Synthetic reception clocks/payloads only. Replay never reads live state."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("history_test", PROJECT / "app/news_history.py")
assert SPEC is not None and SPEC.loader is not None
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)
CUTOFF = datetime(2026, 9, 8, 0, 15, tzinfo=timezone.utc)


class NewsHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="news-time-offline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "project"
        self.root.mkdir()
        self.folder = self.root / "inbox/jd"
        self.records = self.folder / "received"
        self.addCleanup(patch.stopall)
        self.network = patch.object(urllib.request, "urlopen", side_effect=AssertionError("network forbidden")).start()
        self.socket = patch.object(socket, "socket", side_effect=AssertionError("network forbidden")).start()
        self.db = patch.object(sqlite3, "connect", side_effect=AssertionError("any database access forbidden")).start()

    def tearDown(self):
        self.network.assert_not_called()
        self.socket.assert_not_called()
        self.db.assert_not_called()

    def payload(self, title="合成新闻", **extra):
        return {"source": "jd", "as_of": "2026-09-07", "items": [{"title": title}], **extra}

    def receive(self, data, at=CUTOFF-timedelta(minutes=1)):
        raw = json.dumps(data, ensure_ascii=False, indent=1).encode()
        with patch.object(history, "utc_now", return_value=at):
            meta = history.capture(self.root, raw, "2026-09-07")
        return raw, meta

    def tree(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_capture_preserves_exact_bytes_hash_and_private_modes(self):
        data = self.payload(received_at="2000-01-01T00:00:00Z", produced_at="2099-01-01T00:00:00Z")
        raw, meta = self.receive(data)
        self.assertEqual(meta["received_at"], (CUTOFF-timedelta(minutes=1)).isoformat())
        self.assertEqual(meta["body_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual((self.folder/"digest-latest.json").read_bytes(), raw)
        self.assertEqual((self.folder/"digest-2026-09-07.json").read_bytes(), raw)
        self.assertEqual(self.records.stat().st_mode & 0o777, 0o700)
        for path in self.folder.rglob("*.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(history.latest_receipt(self.root, raw.decode())["received_at"], meta["received_at"])

    def test_cutoff_selects_earlier_version_not_current_or_dated_alias(self):
        early, _ = self.receive(self.payload("当时已经收到的版本"))
        self.receive(self.payload("后来才收到的修订"), CUTOFF+timedelta(hours=2))
        for path in self.records.glob("*.json"):
            os.utime(path, (1, 1))  # mtime supplies no historical knowledge.
        selected, _ = history.select_before(self.root, CUTOFF)
        self.assertEqual(selected.encode(), early)
        before = self.tree()
        first = history.replay(self.root, CUTOFF)
        self.assertEqual(first, history.replay(self.root, CUTOFF))
        self.assertIn("当时已经收到的版本", first["text"])
        self.assertNotIn("后来才收到的修订", first["text"])
        self.assertEqual(first["mode"], "news_only_as_of")
        self.assertEqual(before, self.tree())
        self.assertFalse((self.root/"unused.db").exists())

    def test_legacy_files_and_sender_received_at_cannot_create_history(self):
        self.folder.mkdir(parents=True)
        payload = self.payload("旧文件不能冒充当时已知", received_at="2000-01-01T00:00:00Z")
        for name in ("digest-latest.json", "digest-2026-09-07.json"):
            path = self.folder/name
            path.write_text(json.dumps(payload))
            os.utime(path, (1, 1))
        with self.assertRaisesRegex(history.HistoryError, "history_missing"):
            history.select_before(self.root, CUTOFF)

    def test_old_publication_arriving_late_is_not_eligible(self):
        self.receive(self.payload(items=[{"title": "迟到消息", "published_at": "2026-09-07T15:00:00+08:00"}]), CUTOFF+timedelta(seconds=1))
        with self.assertRaisesRegex(history.HistoryError, "no_record_before_cutoff"):
            history.select_before(self.root, CUTOFF)

    def test_exact_reception_boundary_and_timezone_equivalence(self):
        self.receive(self.payload(), CUTOFF)
        cn = CUTOFF.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(history.select_before(self.root, CUTOFF), history.select_before(self.root, cn))
        with self.assertRaisesRegex(history.HistoryError, "no_record_before_cutoff"):
            history.select_before(self.root, CUTOFF-timedelta(microseconds=1))
        with self.assertRaisesRegex(history.HistoryError, "cutoff_invalid"):
            history.select_before(self.root, CUTOFF.replace(tzinfo=None))

    def test_publication_cutoff_excludes_future_but_keeps_exact_boundary(self):
        self.receive(self.payload(items=[
            {"title": "已发布", "published_at": "2026-09-08T08:15:00+08:00"},
            {"title": "未来发布不得展示", "published_at": "2026-09-08T08:15:00.000001+08:00"},
        ]))
        result = history.replay(self.root, CUTOFF)
        self.assertIn("已发布", result["text"])
        self.assertNotIn("未来发布不得展示", result["text"])
        self.assertIn("jd:future", result["issues"])
        self.assertEqual(result["source"]["excluded_time_count"], 1)

    def test_unknown_publication_is_explicit_but_receipt_proves_availability(self):
        self.receive(self.payload())
        result = history.replay(self.root, CUTOFF)
        self.assertIn("合成新闻", result["text"])
        self.assertIn("发布时间未知", result["text"])
        self.assertEqual(result["source"]["publication_unknown_count"], 1)
        self.assertEqual(result["source"]["receipt_status"], "verified")

    def test_null_and_empty_publication_are_unknown_not_invented_or_invalid(self):
        self.receive(self.payload(items=[
            {"title": "显式空值", "published_at": None},
            {"title": "嵌套空文本", "provenance": {"published_at": " "}},
            {"title": "字段缺失"},
        ]))
        result = history.replay(self.root, CUTOFF)
        self.assertEqual(result["source"]["publication_unknown_count"], 3)
        self.assertEqual(result["text"].count("发布时间未知"), 3)
        self.assertNotIn("jd:invalid", result["issues"])

    def test_date_only_publication_never_gets_invented_midnight(self):
        self.receive(self.payload(items=[{"title": "仅提供日期", "published_at": "2026-09-08"}]))
        text = history.replay(self.root, CUTOFF)["text"]
        self.assertIn("2026-09-08（仅日期）", text)
        self.assertNotIn("00:00", text)

    def test_naive_or_invalid_publication_is_not_guessed_in_strict_replay(self):
        self.receive(self.payload(items=[
            {"title": "时区不明不得纳入", "published_at": "2026-09-08T08:00:00"},
            {"title": "时间无效不得纳入", "published_at": "not-a-date"},
            {"title": "UTC来源可核对", "published_at": "2026-09-08T00:14:00Z"},
        ]))
        result = history.replay(self.root, CUTOFF)
        self.assertNotIn("不得纳入", result["text"])
        self.assertIn("UTC来源可核对", result["text"])
        self.assertIn("2026-09-08 08:14:00", result["text"])
        self.assertIn("jd:invalid", result["issues"])

    def test_nested_publication_is_not_shadowed_by_data_date(self):
        self.receive(self.payload(items=[{"as_of": "2026-09-07", "provenance": {
            "title": "明确嵌套发布时间", "published_at": "2026-09-08T00:14:00Z"}}]))
        text = history.replay(self.root, CUTOFF)["text"]
        self.assertIn("发布 2026-09-08 08:14:00", text)
        self.assertNotIn("发布时间未知", text)

    def test_source_date_is_not_misrepresented_as_publication(self):
        self.receive(self.payload(items=[{"title": "数据日期不是发布日", "as_of": "2026-09-07"}]))
        text = history.replay(self.root, CUTOFF)["text"]
        self.assertIn("发布时间未知", text)
        self.assertIn("条目数据时间 2026-09-07（仅日期）", text)

    def test_corrupt_selected_record_does_not_fall_back_to_older_success(self):
        self.receive(self.payload("较旧合成版本"), CUTOFF-timedelta(minutes=2))
        self.receive(self.payload("最新合成版本"))
        path = sorted(self.records.glob("*.json"))[-1]
        value = json.loads(path.read_text())
        value["body_utf8"] = json.dumps(self.payload("篡改正文"))
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(history.HistoryError, "body_mismatch"):
            history.replay(self.root, CUTOFF)

    def test_receipt_timestamp_must_match_receiver_filename(self):
        self.receive(self.payload())
        path = next(self.records.glob("*.json"))
        value = json.loads(path.read_text())
        value["received_at"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(history.HistoryError, "receipt_invalid"):
            history.select_before(self.root, CUTOFF)

    def test_latest_pointer_cannot_attest_different_body(self):
        raw, _ = self.receive(self.payload())
        with self.assertRaisesRegex(history.HistoryError, "body_mismatch"):
            history.latest_receipt(self.root, raw.decode()+" ")

    def test_symlink_and_oversized_receipts_are_rejected(self):
        self.receive(self.payload())
        record = next(self.records.glob("*.json"))
        outside = self.root.parent/"outside.json"
        outside.write_text("PRIVATE_MARKER")
        original = record.read_bytes()
        record.unlink()
        record.symlink_to(outside)
        with self.assertRaisesRegex(history.HistoryError, "path_invalid"):
            history.select_before(self.root, CUTOFF)
        record.unlink()
        record.write_bytes(original)
        with patch.object(history, "MAX_RECORD_BYTES", 32):
            with self.assertRaisesRegex(history.HistoryError, "input_too_large"):
                history.select_before(self.root, CUTOFF)

    def test_ambiguous_clock_and_scan_limit_fail_closed(self):
        self.receive(self.payload("同一时刻版本甲"))
        self.receive(self.payload("同一时刻版本乙"))
        with self.assertRaisesRegex(history.HistoryError, "ambiguous_reception"):
            history.select_before(self.root, CUTOFF)
        with patch.object(history, "MAX_RECORDS", 1):
            with self.assertRaisesRegex(history.HistoryError, "history_limit"):
                history.select_before(self.root, CUTOFF)

    def test_cli_metadata_has_no_raw_payload_and_is_read_only(self):
        # Keep the fake credential in runtime test data, not a secret-like literal.
        private_marker = "SYNTHETIC_PRIVATE_MARKER"
        self.receive(self.payload(f"合成正文 secret={private_marker}"))
        before = self.tree()
        output = io.StringIO()
        with redirect_stdout(output):
            rc = history.main(["--base", str(self.root), "--cutoff", CUTOFF.isoformat()])
        self.assertEqual(rc, 0)
        self.assertNotIn(private_marker, output.getvalue())
        self.assertNotIn("合成正文", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["mode"], "news_only_as_of")
        self.assertEqual(before, self.tree())

    def test_cli_unavailable_does_not_fabricate_a_legacy_receipt(self):
        before = self.tree()
        output = io.StringIO()
        with redirect_stdout(output):
            rc = history.main(["--base", str(self.root), "--cutoff", CUTOFF.isoformat()])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "unavailable")
        self.assertEqual(before, self.tree())


if __name__ == "__main__":
    unittest.main()
