"""Full intake/render agreement, real immutable store, synthetic signed-calendar data."""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_sealed_research import content, payload

validator = content.research_receipts.research_contract
receipts = content.research_receipts
NOW = datetime.fromisoformat("2026-09-25T08:15:00+08:00")


def raw(data):
    return json.dumps(data, ensure_ascii=False).encode()


def digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def skip_payload():
    calendar = {"source_id": "akshare:sina:trade-calendar", "target_date": "2026-09-25",
                "status": "non_trading_day", "previous_trading_date": "2026-09-24",
                "next_trading_date": "2026-09-28", "data_as_of": "2026-09-25T07:50:00+08:00"}
    return {"source": "nng8", "signal_type": "research_skip", "target_date": "2026-09-25",
            "as_of": "2026-09-25", "produced_at": "2026-09-25T08:00:30+08:00",
            "research_skip": {"version": 1, "status": "not_scheduled", "reason": "non_trading_day",
                "market": "XSHG", "target_date": "2026-09-25", "validated_at": "2026-09-25T08:00:30+08:00",
                "calendar": calendar, "proof": {"calendar_sha256": digest(calendar),
                    "occurrence_sha256": "1" * 64, "last_run_sha256": "2" * 64, "context_sha256": "3" * 64}}}


def v2_payload():
    data = payload(NOW)
    summary = data["research_summary"]
    summary["evidence"] = ["[来源 2] [陈旧] 角色：报告期利润同比；利润增长"]
    summary["counter_evidence"] = []
    limitation = "未提供结构化反证来源；此限制只约束本条推断，不否定已核验事实。"
    summary["limitations"].append("推断限制：" + limitation)
    summary["analysis_contract"] = {"version": 2, "claim_type": "inference", "inference_status": "unverified",
        "counter_evidence_status": "explicitly_missing", "inference_limit": limitation,
        "evidence_roles": [{"source_id": "2", "role": "报告期利润同比"}],
        "period_note": "行情 2026-09-24；利润同比报告期 2026-06-30"}
    summary["limitations"].append("期间：" + summary["analysis_contract"]["period_note"] + "。")
    return data


class ResearchContractTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW if tz is None else NOW.astimezone(tz)
        p = patch.object(receipts, "datetime", Clock); p.start(); self.addCleanup(p.stop)

    def renderer(self):
        return content.Renderer(self.base, self.base / "unused.db", NOW)

    def test_invalid_contract_cannot_poison_canonical_day(self):
        for key, value in (("status", "failed"), ("version", True), ("publication_gate", "blocked"),
                           ("data_status", "unknown"), ("report_sha256", "bad"),
                           ("completed_at", "2026-09-25T09:00:00+08:00")):
            data = payload(NOW); data["research_contract"][key] = value
            with self.subTest(key=key):
                with self.assertRaises(receipts.InvalidRecord):
                    receipts.capture(self.base, raw(data))
                self.assertEqual(list(self.base.iterdir()), [])
        valid = payload(NOW)
        ack = receipts.capture(self.base, raw(valid))
        self.assertTrue(ack["stored"])
        renderer = self.renderer(); self.assertIn("合成研究结论", "\n".join(renderer.highlights("nng8")))
        self.assertEqual(renderer.issues, [])

    def test_invalid_summary_rejected_before_reservation(self):
        for mutate in (
            lambda s: s.update(conclusion="missing claim label"),
            lambda s: s.update(sources=[]),
            lambda s: s.update(evidence=["[来源 99] missing source"]),
            lambda s: s.update(counter_evidence=[]),
            lambda s: s.update(limitations=[]),
            lambda s: s.update(risks=["x" * 1201]),
        ):
            data = payload(NOW); mutate(data["research_summary"])
            with self.assertRaises(receipts.InvalidRecord):
                receipts.capture(self.base, raw(data))
            self.assertEqual(list(self.base.iterdir()), [])

    def test_role_period_staleness_and_unverified_inference_survive_rendering(self):
        data = v2_payload(); before = copy.deepcopy(data)
        receipts.capture(self.base, raw(data))
        renderer = self.renderer(); rendered = "\n".join(renderer.highlights("nng8"))
        self.assertEqual(renderer.issues, [])
        for required in ("报告期利润同比", "[陈旧]", "行情 2026-09-24；利润同比报告期 2026-06-30",
                         "推断状态：未核验", data["research_summary"]["analysis_contract"]["inference_limit"]):
            self.assertIn(required, rendered)
        self.assertNotIn("\n反证：", rendered)
        self.assertEqual(data, before)

    def test_header_distinguishes_report_date_from_market_data_date(self):
        data = payload(NOW)
        self.assertEqual(data["research_contract"]["report_date"], "2026-09-25")
        self.assertEqual(data["as_of"], "2026-09-24")
        receipts.capture(self.base, raw(data))
        report = self.renderer().report()
        self.assertIn("研报日 2026-09-25", report["text"])
        self.assertIn("报告日 2026-09-25；行情截至 2026-09-24", report["text"])
        self.assertNotIn("研报日 2026-09-24", report["text"])
        self.assertEqual(report["sources"]["nng8"]["report_date"], "2026-09-25")

    def test_v2_cannot_claim_verified_or_invent_counter_evidence(self):
        for mutate in (
            lambda a: a.update(inference_status="verified"),
            lambda a: a.update(counter_evidence_status="provided"),
            lambda a: a.update(inference_limit="arbitrary exemption"),
            lambda a: a.update(evidence_roles=[{"source_id": "99", "role": "上涨占优"}]),
            lambda a: a.update(claim_type="fact"),
            lambda a: a.update(period_note="行情 2026-02-30；利润同比报告期 2026-06-30"),
            lambda a: a.update(period_note="行情 2026-09-25；利润同比报告期 2026-06-30"),
        ):
            data = v2_payload(); mutate(data["research_summary"]["analysis_contract"])
            with self.assertRaises(receipts.InvalidRecord):
                receipts.capture(self.base, raw(data))
        data = v2_payload(); data["research_summary"]["limitations"] = ["other limitation"]
        with self.assertRaises(receipts.InvalidRecord):
            receipts.capture(self.base, raw(data))

    def test_legacy_risk_placeholder_displayed_as_limitation_not_counter(self):
        data = payload(NOW); summary = data["research_summary"]
        summary["conclusion"] = "[推断] 风险：来源有限"
        summary["counter_evidence"] = ["未提供结构化反证来源，不升级为事实结论。"]
        receipts.capture(self.base, raw(data))
        renderer = self.renderer(); rendered = "\n".join(renderer.highlights("nng8"))
        self.assertEqual(renderer.issues, [])
        self.assertNotIn("\n反证：", rendered)
        self.assertIn("来源限制：", rendered)

    def test_valid_weekday_skip_is_stored_and_consumed_without_false_missing(self):
        data = skip_payload(); ack = receipts.capture(self.base, raw(data))
        self.assertEqual(ack["payload_kind"], "skip")
        self.assertEqual(ack["skip_sha256"], digest(data["research_skip"]))
        renderer = self.renderer(); rendered = "\n".join(renderer.highlights("nng8"))
        self.assertIn("非交易日证明", rendered)
        self.assertEqual(renderer.issues, [])
        self.assertEqual(renderer.sources["nng8"]["status"], "not_scheduled")
        self.assertNotIn("report_sha256", ack)
        report = self.renderer().report()
        self.assertIn("研报日 非交易日无新研究", report["text"])
        self.assertIn("当日为 A 股非交易日", report["text"])
        self.assertIn("日历来源 akshare:sina:trade-calendar", report["text"])
        self.assertNotIn("周末", report["text"])

    def test_weekend_without_skip_retains_explicit_weekend_schedule(self):
        saturday = datetime.fromisoformat("2026-09-26T08:15:00+08:00")
        report = content.Renderer(self.base, self.base / "unused.db", saturday).report()
        self.assertIn("研报日 周末无新研究", report["text"])
        self.assertIn("周末未计划生成新研究", report["text"])
        self.assertNotIn("nng8:missing", report["issues"])

    def test_missing_old_or_unsigned_skip_cannot_become_holiday(self):
        renderer = self.renderer(); renderer.highlights("nng8")
        self.assertIn("nng8:missing", renderer.issues)
        folder = self.base / "inbox/nng8"; folder.mkdir(parents=True)
        for data in (payload(NOW.replace(day=24)), skip_payload()):
            (folder / "digest-latest.json").write_bytes(raw(data))
            renderer = self.renderer(); renderer.highlights("nng8")
            self.assertIn("nng8:invalid", renderer.issues)
            self.assertNotEqual(renderer.sources["nng8"]["status"], "not_scheduled")

    def test_skip_proof_date_source_market_time_and_hash_are_required(self):
        for mutate in (
            lambda d: d.update(target_date="2026-09-24"),
            lambda d: d.update(produced_at="2026-09-25T09:00:00+08:00"),
            lambda d: d["research_skip"].update(market="NYSE"),
            lambda d: d["research_skip"].update(target_date="2026-09-24"),
            lambda d: d["research_skip"]["calendar"].update(source_id="unknown"),
            lambda d: d["research_skip"]["calendar"].update(data_as_of="2026-09-25"),
            lambda d: d["research_skip"]["calendar"].update(previous_trading_date="2026-09-25"),
            lambda d: d["research_skip"]["proof"].update(calendar_sha256="a" * 64),
            lambda d: d["research_skip"].update(proof={}),
        ):
            data = skip_payload(); mutate(data)
            with self.assertRaises(receipts.InvalidRecord):
                receipts.capture(self.base, raw(data))
            self.assertEqual(list(self.base.iterdir()), [])

    def test_skip_does_not_overwrite_ready_report_or_change_original_receipt(self):
        ready = payload(NOW); ack = receipts.capture(self.base, raw(ready))
        with self.assertRaises(receipts.ContentConflict):
            receipts.capture(self.base, raw(skip_payload()))
        self.assertEqual(receipts.read_record(self.base, "nng8", "2026-09-25")[1], ack)

    def test_historical_record_reads_do_not_reapply_new_current_day_rule(self):
        data = payload(NOW); receipts.capture(self.base, raw(data))
        with patch.object(validator, "validate", side_effect=AssertionError("historical read must not reinterpret")):
            self.assertEqual(receipts.read_record(self.base, "nng8", "2026-09-25")[0], raw(data))


if __name__ == "__main__":
    unittest.main()
