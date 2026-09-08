"""Synthetic fixtures only; renderer reads are constrained to each temporary tree."""
from datetime import datetime, timezone
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
from urllib.parse import unquote, urlsplit
import urllib.request


MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "digest_content.py"
SPEC = importlib.util.spec_from_file_location("signal_gate_digest_content_test", MODULE_PATH)
content = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(content)
REAL_CONNECT = sqlite3.connect
REAL_OPEN = io.open
NOW = datetime(2026, 9, 8, 8, tzinfo=content.TZ)


class DigestContentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="digest-content-offline-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.db = self.base / "data" / "signals.db"

    def source(self, name, data):
        path = self.base / "inbox" / name / "digest-latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def database(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with REAL_CONNECT(self.db) as conn:
            conn.execute("""CREATE TABLE pending_signals (
                id INTEGER PRIMARY KEY, idempotency_key TEXT, symbol TEXT,
                review_status TEXT, decision TEXT, quant_status TEXT,
                expires_at TEXT, body_json TEXT, received_at INTEGER,
                reviewed_at INTEGER)""")

    def row(self, key="signal-1", **changes):
        row = dict(idempotency_key=key, symbol="600000.SH", review_status="pending",
                   decision=None, quant_status=None, expires_at="2026-09-08T16:00:00+08:00",
                   body_json=json.dumps({"side": "long"}), received_at=int(NOW.timestamp()),
                   reviewed_at=None)
        row.update(changes)
        with REAL_CONNECT(self.db) as conn:
            conn.execute("INSERT INTO pending_signals (" + ",".join(row) + ") VALUES (" + ",".join("?" for _ in row) + ")", list(row.values()))

    def complete(self, date="2026-09-07"):
        self.database()
        for name in ("nng8", "jd", "quant"):
            self.source(name, {"as_of": date, "items": []})

    def render(self, now=NOW):
        def confined_open(path, mode="r", *args, **kwargs):
            resolved = Path(path).resolve()
            self.assertTrue(resolved.is_relative_to(self.base), "read escaped fixtures")
            self.assertFalse(any(ch in mode for ch in "wax+"), "renderer attempted a write")
            return REAL_OPEN(path, mode, *args, **kwargs)

        def readonly_connect(database, *args, **kwargs):
            self.assertTrue(kwargs.get("uri"), "database not opened as URI")
            parts = urlsplit(database)
            self.assertEqual(parts.query, "mode=ro")
            self.assertTrue(Path(unquote(parts.path)).resolve().is_relative_to(self.base))
            return REAL_CONNECT(database, *args, **kwargs)

        with patch.object(io, "open", side_effect=confined_open), \
             patch.object(sqlite3, "connect", side_effect=readonly_connect), \
             patch.object(socket, "socket", side_effect=AssertionError("network forbidden")) as sockets, \
             patch.object(urllib.request, "urlopen", side_effect=AssertionError("network forbidden")) as network:
            report = content.build_report(self.base, self.db, now)
        sockets.assert_not_called()
        network.assert_not_called()
        self.assertIsInstance(report["degraded"], bool)
        self.assertEqual(report["degraded"], bool(report["issues"]))
        for issue in report["issues"]:
            source, code = issue.split(":")
            self.assertIn(source, content.LABELS)
            self.assertIn(code, content.WARNINGS)
            self.assertTrue(issue.isascii())
        return report

    def test_missing_is_unknown_and_does_not_create_database(self):
        report = self.render()
        self.assertEqual(set(report["issues"]), {"review:missing", "nng8:missing", "jd:missing", "quant:missing"})
        self.assertIn("有效待审总量未知", report["text"])
        self.assertFalse(self.db.exists())

    def test_report_date_is_shanghai_even_when_now_is_utc(self):
        self.complete()
        report = self.render(datetime(2026, 9, 7, 17, tzinfo=timezone.utc))
        self.assertEqual(report["report_date"], "2026-09-08")

    def test_previous_calendar_day_is_normal_and_explicit_empty_is_zero(self):
        self.complete()
        report = self.render()
        self.assertFalse(report["degraded"])
        self.assertIn("当前有效待审 0 条", report["text"])
        self.assertIn("本次上报0条", report["text"])
        self.assertIn("2026-09-07 00:00", report["text"])

    def test_stale_2000_not_hidden_by_fresh_mtime_or_generated_time(self):
        self.complete()
        path = self.source("nng8", {"as_of": "2000-01-01", "generated_at": NOW.isoformat(), "items": []})
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))
        report = self.render()
        self.assertIn("nng8:stale", report["issues"])
        self.assertIn("2000-01-01", report["text"])

    def test_old_item_date_is_labeled_historical_even_with_fresh_envelope(self):
        self.complete()
        self.source("jd", {"as_of": "2026-09-07", "items": [{"title": "历史新闻", "published_at": "2000-01-01"}]})
        report = self.render()
        self.assertIn("jd:stale", report["issues"])
        self.assertIn("含过期历史内容，不作新增", report["text"])
        self.assertIn("2000-01-01", report["text"])

    def test_weekend_72_hour_boundary(self):
        monday = datetime(2026, 9, 7, 8, tzinfo=content.TZ)
        self.complete("2026-09-04T08:00:00+08:00")
        self.assertFalse(self.render(monday)["degraded"])
        self.source("jd", {"as_of": "2026-09-04T07:59:59+08:00", "items": []})
        self.assertIn("jd:stale", self.render(monday)["issues"])

    def test_future_invalid_and_undated_are_distinct(self):
        self.complete()
        self.source("nng8", {"as_of": "2026-09-09", "items": []})
        self.source("jd", {"as_of": "2026-99-99", "items": []})
        self.source("quant", {"items": []})
        issues = self.render()["issues"]
        self.assertIn("nng8:future", issues)
        self.assertIn("jd:invalid", issues)
        self.assertIn("quant:unknown_date", issues)

    def test_markdown_source_date_and_no_more_than_three_bullets(self):
        self.database()
        path = self.base / "inbox" / "nng8" / "digest-latest.md"
        path.parent.mkdir(parents=True)
        path.write_text("as_of: 2026-09-07\n" + "\n".join(f"- 要点{n}" for n in range(8)))
        report = self.render()
        self.assertEqual(report["sources"]["nng8"]["status"], "ok")
        self.assertIn("要点2", report["text"])
        self.assertNotIn("要点3", report["text"])

    def test_invalid_json_only_degrades_its_section(self):
        self.complete()
        self.source("jd", {"as_of": "2026-09-07", "items": [{"title": "有效资讯"}]})
        path = self.base / "inbox" / "nng8" / "digest-latest.json"
        path.write_text("{invalid")
        report = self.render()
        self.assertIn("nng8:invalid", report["issues"])
        self.assertIn("有效资讯", report["text"])

    def test_pending_filters_expired_unknown_and_noise_with_body_side(self):
        self.complete()
        self.row()
        self.row("old", expires_at="2026-09-08T07:59:00+08:00")
        self.row("unknown", expires_at="not-a-date")
        self.row("selftest:1")
        self.row("simulated", body_json=json.dumps({"simulation": True}))
        text = self.render()["text"]
        self.assertIn("当前有效待审 1 条；已过期 1 条", text)
        self.assertIn("有效期未知 1 条", text)
        self.assertIn("过滤模拟/探针 2 条", text)
        self.assertIn("偏多", text)

    def test_review_daily_and_all_time_are_not_conflated(self):
        self.complete()
        yesterday = int(datetime(2026, 9, 7, 9, tzinfo=content.TZ).timestamp())
        self.row("previous", review_status="reviewed", decision="approve", quant_status="accepted", received_at=yesterday, reviewed_at=yesterday)
        text = self.render()["text"]
        self.assertIn("今日新增 0 条；今日审核 0 条", text)
        self.assertIn("累计（全部本地留存）：通过 1 条", text)
        self.assertIn("已提交/受理，非成交", text)

    def test_missing_columns_do_not_claim_zero_or_break_other_sections(self):
        self.db.parent.mkdir(parents=True)
        with REAL_CONNECT(self.db) as conn:
            conn.execute("CREATE TABLE pending_signals (id INTEGER, body_json TEXT)")
            conn.execute("INSERT INTO pending_signals VALUES (1, 'invalid')")
        self.source("jd", {"as_of": "2026-09-07", "items": [{"title": "仍显示资讯"}]})
        report = self.render()
        self.assertIn("review:schema_error", report["issues"])
        self.assertIn("仍显示资讯", report["text"])
        self.assertIn("累计人审：未知", report["text"])
        self.assertNotIn("当前有效待审 0", report["text"])
        self.assertNotIn("通过 0", report["text"])
        self.assertNotIn("拒绝 0", report["text"])

    def test_invalid_body_with_compatible_schema_is_section_degradation(self):
        self.complete()
        self.row(body_json="invalid", received_at=None)
        report = self.render()
        self.assertIn("review:invalid", report["issues"])
        self.assertIn("今日新增 未知", report["text"])

    def test_missing_decision_or_status_is_unavailable_not_zero(self):
        for missing in ("decision", "review_status"):
            with self.subTest(missing=missing):
                if self.db.exists():
                    self.db.unlink()
                self.database()
                with REAL_CONNECT(self.db) as conn:
                    conn.execute(f"ALTER TABLE pending_signals DROP COLUMN {missing}")
                text = self.render()["text"]
                self.assertIn("累计人审：未知", text)
                self.assertNotIn("当前有效待审 0", text)

    def test_truncated_reviews_never_claim_full_pending_total(self):
        self.complete()
        self.row("first")
        self.row("second")
        with patch.object(content, "MAX_ROWS", 1):
            report = self.render()
        self.assertIn("review:truncated", report["issues"])
        self.assertIn("有效待审总量未知", report["text"])
        self.assertIn("最近1条中，当前有效待审 1 条", report["text"])

    def test_missing_table_is_schema_error_not_empty_zero(self):
        self.db.parent.mkdir(parents=True)
        REAL_CONNECT(self.db).close()
        report = self.render()
        self.assertIn("review:schema_error", report["issues"])
        self.assertIn("累计人审：未知", report["text"])

    def test_execution_totals_use_same_filtered_items_not_raw_summary(self):
        self.complete()
        self.source("quant", {"as_of": "2026-09-07", "summary": {"accepted": 99, "fills": 88}, "items": [
            {"idempotency_key": "selftest:1", "status": "accepted"},
            {"idempotency_key": "paper-1", "simulation": True, "status": "filled"},
            {"idempotency_key": "reviewtest", "status": "accepted", "symbol": "KEEP"},
            {"status": "submitted"}, {"status": "rejected"}, {"status": "filled"},
            {"status": "something-else"},
        ]})
        text = self.render()["text"]
        self.assertIn("受理 1 / 提交 1 / 拒绝 1 / 上报成交 1 / 状态未知 1", text)
        self.assertIn("排除模拟/探针 2 条", text)
        self.assertNotIn("受理 99", text)
        self.assertNotIn("成交 88", text)
        self.assertIn("KEEP", text)
        self.assertIn("来源性质未独立核实", text)

    def test_summary_zero_is_not_unknown_and_not_claimed_disabled(self):
        self.complete()
        self.source("quant", {"as_of": "2026-09-07", "summary": {"accepted": 0, "fills": 0}})
        text = self.render()["text"]
        self.assertIn("受理 0 / 拒绝 未知 / 成交 0", text)
        self.assertIn("模拟占比未知", text)
        self.assertNotIn("成交与盈亏：未启用", text)

    def test_explicit_simulation_and_noise_filter_highlights(self):
        self.complete()
        self.source("jd", {"as_of": "2026-09-07", "items": [
            {"title": "FILTER1", "simulation": "true"},
            {"title": "FILTER2", "idempotency_key": "research_eod:nng8:probe:1"},
            {"title": "KEEP", "idempotency_key": "reviewtest"},
        ]})
        text = self.render()["text"]
        self.assertNotIn("FILTER", text)
        self.assertIn("KEEP", text)

    def test_long_text_control_characters_and_private_links_are_not_forwarded(self):
        self.complete()
        self.source("jd", {"as_of": "2026-09-07", "items": [
            {"title": "开头\u202e\n" + "长" * 10000},
            {"title": "查看 https://example.com/api?token=PRIVATE_VALUE api_key=HIDDEN_VALUE", "url": "https://user:secret@example.com/private"},
            {"title": "公开新闻", "url": "https://example.com/news/123", "published_at": "2026-09-07T14:30:00+08:00"},
        ]})
        text = self.render()["text"]
        self.assertLess(len(text), 6000)
        self.assertNotIn("\u202e", text)
        self.assertNotIn("长" * 200, text)
        self.assertNotIn("PRIVATE_VALUE", text)
        self.assertNotIn("HIDDEN_VALUE", text)
        self.assertNotIn("user:secret", text)
        self.assertIn("https://example.com/news/123", text)
        self.assertIn("09-07 14:30", text)

    def test_oversized_source_is_not_read_without_bound(self):
        self.complete()
        path = self.base / "inbox" / "nng8" / "digest-latest.json"
        path.write_text("x" * (content.MAX_INPUT + 1))
        self.assertIn("nng8:too_large", self.render()["issues"])

    def test_raw_credential_shapes_and_opaque_private_paths_are_hidden(self):
        self.complete()
        examples = ["sk" + "-" + "a" * 28, "ghp" + "_" + "B" * 30,
                    "12345678" + ":" + "C" * 35,
                    "eyJ" + "D" * 12 + "." + "E" * 12 + "." + "F" * 12]
        opaque = "abcd" * 4 + "1234" * 4
        uuid = "a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12
        self.source("jd", {"as_of": "2026-09-07", "items": [
            {"title": " ".join(examples)},
            {"title": "https://example.com/" + opaque},
            {"title": "入口", "url": "https://example.com/" + uuid},
        ]})
        text = self.render()["text"]
        for secret in examples + [opaque, uuid]:
            self.assertNotIn(secret, text)
        self.assertEqual(content.safe_url("https://example.com/%74oken/private"), "")

    def test_symlink_source_is_rejected_without_reading_target(self):
        self.database()
        private = self.base / "secrets.env"
        private.write_text("NOT_TO_BE_READ")
        path = self.base / "inbox" / "nng8" / "digest-latest.json"
        path.parent.mkdir(parents=True)
        path.symlink_to(private)
        report = self.render()
        self.assertIn("nng8:invalid", report["issues"])
        self.assertNotIn("NOT_TO_BE_READ", report["text"])

    def test_database_remains_byte_identical(self):
        self.complete()
        self.row()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.render()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)
        self.assertFalse(Path(str(self.db) + "-journal").exists())

    def test_invalid_types_degrade_instead_of_raising(self):
        self.complete()
        self.source("jd", {"as_of": "2026-09-07", "items": [None, 10, {"title": {}}, {"title": "valid"}]})
        self.source("quant", {"as_of": "2026-09-07", "items": [{"status": []}, None]})
        report = self.render()
        self.assertIn("jd:schema_error", report["issues"])
        self.assertIn("quant:schema_error", report["issues"])
        self.assertIn("valid", report["text"])
        self.assertIn("状态未知 2", report["text"])


if __name__ == "__main__":
    unittest.main()
