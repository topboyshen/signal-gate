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
            # Legacy input-format coverage remains explicit. Production uses the
            # default sealed-research gate; see test_sealed_research.py.
            report = content.build_report(self.base, self.db, now, require_sealed_research=False)
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

    def test_jd_real_shape_renders_nested_editorial_fields(self):
        self.complete()
        self.source("jd", {"source": "jd", "as_of": "2026-09-07", "items": [
            {"symbol": "SYNTH-A", "side": "long", "confidence": 0.6,
             "signal_type": "news", "event_window": {
                 "start": "2026-09-08", "end": "2026-09-10"},
             "provenance": {"source": "合成公开来源甲", "event_id": "synthetic-event-a",
                            "title": "合成资讯标题甲", "url": "https://example.com/news/a",
                            "published_at": "2026-09-07T14:30:00+08:00"}},
            {"symbol": "SYNTH-B", "side": "", "confidence": 0.5,
             "signal_type": "news", "event_window": {},
             "provenance": {"source": "合成公开来源乙", "event_id": "synthetic-event-b",
                            "summary": "缺标题时使用合成摘要乙"}},
        ]})
        report = self.render()
        text = report["text"]
        for expected in ("合成资讯标题甲", "合成公开来源甲", "https://example.com/news/a",
                         "2026-09-07 14:30", "缺标题时使用合成摘要乙", "偏多"):
            self.assertIn(expected, text)
        self.assertNotIn("synthetic-event-a", text)
        self.assertFalse(report["degraded"])

    def test_jd_valid_top_level_fields_take_precedence_over_provenance(self):
        self.complete()
        self.source("jd", {"as_of": "2026-09-07", "items": [
            {"title": "顶层标题应保留", "source": "顶层公开来源",
             "url": "https://example.com/news/top",
             "published_at": "2026-09-07T15:10:00+08:00",
             "provenance": {"title": "嵌套标题不应覆盖", "source": "嵌套来源不应覆盖",
                            "url": "https://example.com/news/nested",
                            "published_at": "2000-01-01"}},
        ]})
        report = self.render()
        for expected in ("顶层标题应保留", "顶层公开来源", "https://example.com/news/top",
                         "2026-09-07 15:10"):
            self.assertIn(expected, report["text"])
        for omitted in ("嵌套标题不应覆盖", "嵌套来源不应覆盖", "/news/nested", "2000-01-01"):
            self.assertNotIn(omitted, report["text"])
        self.assertNotIn("jd:stale", report["issues"])

    def test_jd_bad_top_level_types_do_not_mask_valid_nested_fields(self):
        self.complete()
        for bad in ({"unexpected": "object"}, ["unexpected list"], True):
            with self.subTest(bad_type=type(bad).__name__):
                self.source("jd", {"as_of": "2026-09-07", "items": [
                    {"title": bad, "source": bad, "url": bad, "published_at": bad,
                     "provenance": {"title": "有效嵌套标题", "source": "有效嵌套来源",
                                    "url": "https://example.com/news/fallback",
                                    "published_at": "2026-09-07T16:20:00+08:00"}},
                ]})
                text = self.render()["text"]
                for expected in ("有效嵌套标题", "有效嵌套来源", "/news/fallback", "2026-09-07 16:20"):
                    self.assertIn(expected, text)

    def test_jd_bad_nested_title_type_allows_valid_nested_summary(self):
        self.complete()
        self.source("jd", {"as_of": "2026-09-07", "items": [
            {"provenance": {"title": {"unexpected": "object"}, "summary": "可用的嵌套摘要"}},
        ]})
        self.assertIn("可用的嵌套摘要", self.render()["text"])

    def test_jd_non_object_provenance_degrades_without_losing_top_level_title(self):
        self.complete()
        for malformed in (None, [], "not-an-object", 7, True):
            with self.subTest(provenance_type=type(malformed).__name__):
                self.source("jd", {"as_of": "2026-09-07", "items": [
                    {"title": "有效顶层资讯", "provenance": malformed},
                ]})
                report = self.render()
                self.assertIn("有效顶层资讯", report["text"])
                self.assertIn("jd:schema_error", report["issues"])

    def test_jd_event_window_is_not_a_news_publication_date(self):
        self.complete()
        self.source("jd", {"as_of": "2026-09-07", "items": [
            {"symbol": "SYNTH-A", "event_window": {"start": "2099-01-01", "end": "2099-02-01"},
             "provenance": {"title": "只有事件窗口，没有新闻发布时间"}},
            {"symbol": "SYNTH-B", "event_window": {"start": "2000-01-01", "end": "2000-02-01"},
             "provenance": {"title": "历史事件窗口也不是发布时间"}},
        ]})
        report = self.render()
        self.assertIn("只有事件窗口，没有新闻发布时间", report["text"])
        self.assertNotIn("2099-01-01", report["text"])
        self.assertNotIn("2000-01-01", report["text"])
        self.assertFalse(report["degraded"])

    def test_jd_nested_publication_date_retains_date_quality_checks(self):
        self.complete()
        for published, expected in (("2000-01-01", "stale"),
                                    ("2099-01-01", "future"),
                                    ("not-a-date", "invalid")):
            with self.subTest(published_at=published):
                self.source("jd", {"as_of": "2026-09-07", "items": [
                    {"provenance": {"title": "带日期的合成新闻", "published_at": published}},
                ]})
                report = self.render()
                self.assertIn("带日期的合成新闻", report["text"])
                self.assertIn(f"jd:{expected}", report["issues"])

    def test_jd_nested_text_source_and_links_keep_existing_redaction(self):
        self.complete()
        credential = "sk" + "-" + "q" * 28
        self.source("jd", {"as_of": "2026-09-07", "items": [
            {"provenance": {
                "title": "合成标题 " + credential + " https://example.com/private?token=NESTED_SECRET",
                "source": "来源 api_key=SOURCE_SECRET",
                "url": "https://user:LINK_SECRET@example.com/private"}},
            {"provenance": {"summary": "合成摘要 secret=SUMMARY_SECRET",
                            "url": "https://example.com/news/public"}},
        ]})
        text = self.render()["text"]
        for secret in (credential, "NESTED_SECRET", "SOURCE_SECRET", "LINK_SECRET", "SUMMARY_SECRET"):
            self.assertNotIn(secret, text)
        for visible in ("合成标题", "合成摘要", "https://example.com/news/public", "已隐藏"):
            self.assertIn(visible, text)

    def test_jd_explicit_nested_simulation_and_probe_markers_are_filtered(self):
        self.complete()
        marker_cases = ({"simulation": True}, {"mode": "paper"},
                        {"idempotency_key": "probe:synthetic"}, {"event_id": "probe:synthetic"})
        for marker in marker_cases:
            with self.subTest(marker=marker):
                self.source("jd", {"as_of": "2026-09-07", "items": [
                    {"title": "FILTER_TOP", "simulation": True,
                     "provenance": {"title": "FILTER_TOP_NESTED"}},
                    {"title": "FILTER_NESTED", "provenance": {"title": "FILTER_INNER", **marker}},
                    {"provenance": {"title": "KEEP probe 普通新闻文字",
                                    "event_id": "synthetic-event-kept"}},
                ]})
                text = self.render()["text"]
                self.assertNotIn("FILTER", text)
                self.assertIn("KEEP probe 普通新闻文字", text)

    def test_nng8_real_text_shape_selects_later_sector_and_watch_content(self):
        self.complete()
        research_lines = [
            "研究要点（截至 2026-09-07 收盘，报告日 2026-09-08）",
            "大盘：合成指数甲上涨，合成指数乙回落。",
            "成交额：合成数据较前日增加。",
            "板块：合成制造板块相对活跃。",
            "观察标的 3 只（非买卖建议）：",
            "- SYNTH-A 合成标的一 昨收 +1.00%",
            "- SYNTH-B 合成标的二 昨收 -1.00%",
            "- SYNTH-C 合成标的三 昨收 +0.00%",
            "数据：晨报 正常（门禁 passed；语义分析 skipped_no_semantic_candidate）",
            "说明：本段只报合成事实，不给仓位建议。",
            "research-data 快照：accepted",
        ]
        self.assertEqual(len(research_lines), 11)
        text = "\n".join(research_lines)
        self.source("nng8", {"source": "nng8", "as_of": "2026-09-07",
                             "produced_at": "2026-09-08T07:40:00+08:00",
                             "text": text, "markdown": text,
                             "signals_count": 0, "signal_type": "research_eod"})
        report = self.render()
        bullets = [line for line in report["text"].splitlines() if line.startswith("- ")]
        self.assertLessEqual(len(bullets), 3)
        self.assertNotIn(research_lines[0], report["text"])
        self.assertIn("合成制造板块相对活跃", report["text"])
        self.assertTrue(any("合成标的一" in line or "合成标的二" in line for line in bullets))
        self.assertFalse(any(line == "- 观察标的 3 只（非买卖建议）：" for line in bullets))
        self.assertFalse(report["degraded"])

    def test_nng8_late_source_limitations_and_rejected_snapshot_are_not_hidden(self):
        self.complete()
        text = "\n".join([
            "研究要点（截至 2026-09-07 收盘，报告日 2026-09-08）",
            "大盘：合成指数小幅上涨。", "成交额：合成成交数据。",
            "板块：合成板块相对活跃。", "观察标的 3 只（非买卖建议）：",
            "- SYNTH-A 合成标的一 昨收 +1.00%", "- SYNTH-B 合成标的二 昨收 -1.00%",
            "- SYNTH-C 合成标的三 昨收 +0.00%",
            "数据：晨报 正常（门禁 passed；语义分析 skipped_no_semantic_candidate）",
            "说明：数据包部分可用；本段只报合成事实，不给仓位建议。",
            "research-data 快照：rejected",
        ])
        self.source("nng8", {"source": "nng8", "as_of": "2026-09-07",
                             "produced_at": "2026-09-08T07:40:00+08:00", "text": text,
                             "markdown": text, "signals_count": 0, "signal_type": "research_eod"})
        report = self.render()
        self.assertIn("数据包部分可用", report["text"])
        self.assertIn("research-data 快照：rejected", report["text"])
        self.assertIn("nng8:invalid", report["issues"])
        self.assertLessEqual(sum(line.startswith("- ") for line in report["text"].splitlines()), 3)

    def test_nng8_risk_after_all_three_sections_is_retained_as_a_source_note(self):
        self.complete()
        for label in ("风险提示", "风险", "限制"):
            with self.subTest(label=label):
                warning = f"{label}：数据缺失，上述观察暂不采用"
                text = "\n".join([
                    "大盘：合成指数小幅上涨。", "板块：合成制造板块相对活跃。",
                    "观察标的 1 只（非买卖建议）：", "- SYNTH-A 合成标的一 昨收 +1.00%",
                    warning,
                ])
                self.source("nng8", {"source": "nng8", "as_of": "2026-09-07", "text": text,
                                     "produced_at": "2026-09-08T07:40:00+08:00",
                                     "signals_count": 0, "signal_type": "research_eod"})
                report = self.render()
                lines = report["text"].splitlines()
                self.assertTrue(any(warning in line and line.startswith("来源提示：") for line in lines))
                self.assertLessEqual(sum(line.startswith("- ") for line in lines), 3)
                self.assertLess(len(report["text"]), 6000)

    def test_nng8_rejected_snapshot_survives_regular_source_note_overflow(self):
        self.complete()
        for note_count in (3, 80):
            with self.subTest(note_count=note_count):
                regular_notes = [f"说明：合成数据备注{number} " + "合成" * 50
                                 for number in range(note_count)]
                text = "\n".join([
                    "大盘：合成指数小幅上涨。", "板块：合成制造板块相对活跃。",
                    "观察标的 1 只（非买卖建议）：", "- SYNTH-A 合成标的一 昨收 +1.00%",
                    *regular_notes, "research-data 快照：rejected",
                ])
                self.source("nng8", {"source": "nng8", "as_of": "2026-09-07", "text": text,
                                     "produced_at": "2026-09-08T07:40:00+08:00",
                                     "signals_count": 0, "signal_type": "research_eod"})
                report = self.render()
                self.assertIn("research-data 快照：rejected", report["text"])
                self.assertIn("nng8:invalid", report["issues"])
                self.assertIn("nng8:truncated", report["issues"])
                self.assertLessEqual(sum(line.startswith("- ") for line in report["text"].splitlines()), 3)
                self.assertLess(len(report["text"]), 6000)

    def test_nng8_unlabelled_text_keeps_original_order_and_three_point_limit(self):
        self.complete()
        text = "\n".join(("普通要点甲", "普通要点乙", "普通要点丙", "普通要点丁"))
        self.source("nng8", {"source": "nng8", "as_of": "2026-09-07", "text": text,
                             "produced_at": "2026-09-08T07:40:00+08:00",
                             "signals_count": 0, "signal_type": "research_eod"})
        report = self.render()
        bullets = [line for line in report["text"].splitlines() if line.startswith("- ")]
        self.assertEqual(bullets, ["- 普通要点甲", "- 普通要点乙", "- 普通要点丙"])
        self.assertNotIn("普通要点丁", report["text"])

    def test_nng8_long_watch_list_never_silently_drops_a_tail_qualification(self):
        self.complete()
        qualification = "以上观察尚未验证，不应作为完整结论采用"
        cases = {
            "one_long_item": ["- SYNTH-A " + "合成观察细节" * 35 + "；" + qualification],
            "merged_short_items": [
                "- SYNTH-A " + "合成观察甲" * 15,
                "- SYNTH-B " + "合成观察乙" * 15,
                "- SYNTH-C " + qualification,
            ],
        }
        for shape, watch_lines in cases.items():
            with self.subTest(shape=shape):
                text = "\n".join([
                    "大盘：合成指数小幅上涨。", "板块：合成板块相对活跃。",
                    "观察标的（非买卖建议）：", *watch_lines,
                ])
                self.source("nng8", {"source": "nng8", "as_of": "2026-09-07",
                                     "text": text, "signals_count": 0,
                                     "signal_type": "research_eod"})
                report = self.render()
                self.assertLessEqual(sum(line.startswith("- ") for line in report["text"].splitlines()), 3)
                if qualification not in report["text"]:
                    self.assertIn("nng8:truncated", report["issues"])
                    self.assertIn("部分段落或观察清单已截断，不能视为完整结论", report["text"])
                    self.assertTrue(report["degraded"])
                self.assertLess(len(report["text"]), 6000)

    def test_nng8_fresh_produced_at_does_not_refresh_old_research_as_of(self):
        self.complete()
        self.source("nng8", {"source": "nng8", "as_of": "2000-01-01",
                             "produced_at": NOW.isoformat(), "text": "板块：历史合成研究内容。",
                             "signals_count": 0, "signal_type": "research_eod"})
        report = self.render()
        self.assertIn("nng8:stale", report["issues"])
        self.assertIn("含过期历史内容，不作新增", report["text"])

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
        self.assertNotIn("今日审核结果：", text)
        self.assertNotIn("600000.SH｜通过", text)

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

    def test_index_codes_are_not_listed_as_stocks(self):
        self.complete()
        self.source("jd", {"as_of": "2026-09-07", "items": [
            {"symbol": "000300.SH", "side": "long"},
            {"symbol": "399006.SZ", "side": "short", "title": "指数层面事件"},
            {"symbol": "600000.SH", "name": "浦发银行", "title": "个股新闻"},
        ]})
        text = self.render()["text"]
        self.assertNotIn("000300.SH", text)
        self.assertNotIn("399006.SZ", text)
        self.assertIn("创业板指", text)
        self.assertIn("指数层面事件", text)
        self.assertIn("浦发银行 600000.SH", text)
        self.assertIn("个股新闻", text)
        self.source("jd", {"as_of": "2026-09-07", "items": [
            {"symbol": "000300.SH",
             "title": "上证指数早盘收报3937点，跌0.35%。 深证成指早盘收报13647点，跌0.55%。"},
            {"symbol": "000688.SH", "title": "科创50ETF吸金47亿"},
        ]})
        recap = self.render()["text"]
        self.assertNotIn("早盘收报3937", recap)
        self.assertIn("科创50ETF吸金47亿", recap)

    def test_research_watch_line_keeps_local_name(self):
        self.complete()
        text = "\n".join([
            "大盘：上证指数 +0.07%。", "板块：元件走强。",
            "观察标的：", "- 002617.SZ 露笑科技 昨收 +0.33%",
        ])
        self.source("nng8", {"source": "nng8", "as_of": "2026-09-07", "text": text,
                             "produced_at": "2026-09-08T07:40:00+08:00"})
        report = self.render()
        self.assertIn("露笑科技 002617.SZ", report["text"])

    def test_index_review_rows_are_ignored(self):
        self.complete()
        self.row("idx", symbol="399006.SZ", review_status="reviewed", decision="approve",
                 received_at=int(NOW.timestamp()), reviewed_at=int(NOW.timestamp()))
        self.row("stock", symbol="600000.SH", review_status="reviewed", decision="approve",
                 received_at=int(NOW.timestamp()), reviewed_at=int(NOW.timestamp()),
                 quant_status="accepted")
        text = self.render()["text"]
        self.assertNotIn("399006.SZ", text)
        self.assertNotIn("创业板指｜通过", text)
        self.assertIn("今日审核结果：", text)
        self.assertIn("600000.SH｜通过", text)
        self.assertIn("累计（全部本地留存）：通过 1 条", text)

    def test_header_names_research_day_and_sealed_missing_keeps_old_text_out(self):
        self.database()
        md = self.base / "inbox" / "nng8" / "digest-latest.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("大盘：上证指数 +0.07%；露笑科技不应出现\n", encoding="utf-8")
        self.source("jd", {"as_of": "2026-09-07", "items": [{"title": "有效资讯"}]})
        self.source("quant", {"as_of": "2026-09-07", "items": []})
        report = content.build_report(self.base, self.db, NOW, require_sealed_research=True)
        self.assertIn("研报日 缺报", report["text"])
        self.assertIn("今日：待审 0 · 研报日 缺报", report["text"])
        self.assertNotIn("不代表缺报来源没有新内容", report["text"])
        self.assertIn("旧稿未纳入", report["text"])
        self.assertNotIn("上证指数 +0.07%", report["text"])
        self.assertNotIn("露笑科技不应出现", report["text"])
        self.assertIn("有效资讯", report["text"])
        self.assertIn("nng8:missing", report["issues"])

    def test_similar_historical_headlines_are_deduplicated(self):
        self.complete()
        self.source("jd", {"as_of": "2026-09-07", "items": [
            {"symbol": "399006.SZ", "title": "收评：创业板指冲高回落跌超1% 农业板块逆势走强"},
            {"symbol": "399006.SZ", "title": "A股收盘：创业板指震荡跌超1%，农业股逆势爆发"},
            {"symbol": "399006.SZ", "title": "9月8日收评：创业板指震荡跌超1%，农业股逆势爆发"},
            {"symbol": "000688.SH", "title": "科创50ETF近5日吸金47亿"},
        ]})
        text = self.render()["text"]
        self.assertEqual(text.count("创业板指震荡跌超1%"), 1)
        self.assertIn("科创50ETF近5日吸金47亿", text)

    def test_execution_drops_index_rows_from_counts_and_details(self):
        self.complete()
        self.source("quant", {"as_of": "2026-09-07", "items": [
            {"symbol": "399006.SZ", "status": "accepted"},
            {"symbol": "600000.SH", "status": "accepted"},
        ]})
        text = self.render()["text"]
        self.assertIn("受理 1 /", text)
        self.assertIn("600000.SH", text)
        self.assertNotIn("创业板指｜受理", text)
        self.assertNotIn("399006.SZ", text)


if __name__ == "__main__":
    unittest.main()
