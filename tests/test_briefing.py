"""Temporary briefing fixtures only; no production inputs or network."""
from datetime import datetime
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

from zoneinfo import ZoneInfo

MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "briefing.py"
SPEC = importlib.util.spec_from_file_location("signal_gate_briefing_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
briefing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(briefing)
TZ = ZoneInfo("Asia/Shanghai")


def ashare(day: str) -> dict:
    text = (
        f"A股联动日报 {day}\n"
        f"生成 08:30（北京时间）｜研报日 2026-11-10\n\n"
        "【今日关注】\n今日：待审 0 · 研报日 2026-11-10\n\n"
        "【研究】\n- 示例研究\n\n【资讯】\n- 示例资讯\n\n"
        "【来源日期与警示】\n说明：缺报不沿用旧稿。\n"
    )
    return {"report_date": day, "text": text, "issues": [], "degraded": False}


class BriefingComposeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="briefing-offline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def compose(self, now: datetime, **files) -> dict:
        for name, body in files.items():
            path = self.root / name
            path.write_text(body, encoding="utf-8")
        return briefing.compose_morning_briefing(ashare(now.date().isoformat()), now, self.root)

    def test_weekday_includes_overseas_and_vps_and_retitles(self):
        now = datetime(2026, 11, 10, 8, 30, tzinfo=TZ)  # Tuesday
        report = self.compose(
            now,
            **{"daily-2026-11-10.md": "🌐 海外市场晨报｜2026-11-10\n纳指收涨。",
               "vps-2026-11-10.txt": "Load: 0.21 / 0.34\n异地备份: OK (auto-backup-20261110-033001)"},
        )
        text = report["text"]
        self.assertTrue(text.startswith("早报 2026-11-10"))
        self.assertNotIn("A股联动日报", text)
        self.assertIn("【今日关注】", text)
        self.assertIn("【海外晨报】\n🌐 海外市场晨报｜2026-11-10\n纳指收涨。", text)
        self.assertIn("【VPS 健康】\nLoad: 0.21 / 0.34", text)
        self.assertNotIn("【海外周报】", text)
        self.assertEqual(report["issues"], [])

    def test_missing_sections_are_labeled_and_do_not_block_or_invent(self):
        now = datetime(2026, 11, 10, 8, 30, tzinfo=TZ)
        text = self.compose(now)["text"]
        self.assertIn("【海外晨报】\n缺报/未生成", text)
        self.assertIn("【VPS 健康】\n缺报/未生成", text)
        self.assertNotIn("纳指", text)
        self.assertNotIn("【海外周报】", text)
        self.assertIn("【今日关注】", text)

    def test_monday_and_sunday_use_no_us_close_note_even_without_files(self):
        monday = datetime(2026, 11, 9, 8, 30, tzinfo=TZ)
        sunday = datetime(2026, 11, 8, 8, 30, tzinfo=TZ)
        for now in (monday, sunday):
            with self.subTest(day=now.date().isoformat()):
                text = briefing.compose_morning_briefing(
                    ashare(now.date().isoformat()), now, self.root
                )["text"]
                self.assertIn("【海外晨报】\n今日无新美股收盘", text)
                self.assertNotIn("缺报/未生成\n\n【VPS", text.split("【海外晨报】")[1].split("【VPS")[0])
                self.assertIn("【VPS 健康】\n缺报/未生成", text)
                self.assertNotIn("【海外周报】", text)

    def test_monday_ignores_unexpected_generated_daily_file(self):
        now = datetime(2026, 11, 9, 8, 30, tzinfo=TZ)
        text = self.compose(now, **{"daily-2026-11-09.md": "不应出现的周一稿"})["text"]
        self.assertIn("今日无新美股收盘", text)
        self.assertNotIn("不应出现的周一稿", text)

    def test_saturday_appends_weekly_or_missing_label(self):
        now = datetime(2026, 11, 7, 8, 30, tzinfo=TZ)  # Saturday
        missing = self.compose(
            now,
            **{"daily-2026-11-07.md": "周二至周六晨报正文",
               "vps-2026-11-07.txt": "Mem: 46%"},
        )["text"]
        self.assertIn("【海外晨报】\n周二至周六晨报正文", missing)
        self.assertIn("【海外周报】\n未生成", missing)
        self.assertIn("【VPS 健康】\nMem: 46%", missing)

        present = self.compose(
            now,
            **{"daily-2026-11-07.md": "晨报正文",
               "weekly-2026-11-07.md": "本周海外复盘",
               "vps-2026-11-07.txt": "Disk: 49%"},
        )["text"]
        self.assertIn("【海外周报】\n本周海外复盘", present)

    def test_symlink_and_escape_are_treated_as_missing(self):
        now = datetime(2026, 11, 10, 8, 30, tzinfo=TZ)
        target = self.root / "secret.txt"
        target.write_text("不应泄漏", encoding="utf-8")
        (self.root / "daily-2026-11-10.md").symlink_to(target)
        text = briefing.compose_morning_briefing(ashare("2026-11-10"), now, self.root)["text"]
        self.assertIn("【海外晨报】\n缺报/未生成", text)
        self.assertNotIn("不应泄漏", text)

    def test_ansi_is_stripped_and_oversized_section_is_truncated_not_blocking(self):
        now = datetime(2026, 11, 10, 8, 30, tzinfo=TZ)
        colored = "\033[0;32mLoad:\033[0m 0.2\n异地备份: OK"
        huge = "x" * (briefing.MAX_SECTION_CHARS + 50)
        text = self.compose(
            now,
            **{"daily-2026-11-10.md": huge, "vps-2026-11-10.txt": colored},
        )["text"]
        self.assertIn("（已截断）", text)
        self.assertIn("【VPS 健康】\nLoad: 0.2\n异地备份: OK", text)
        self.assertNotIn("\033[", text)
        self.assertIn("【今日关注】", text)
        self.assertLess(text.count("x"), briefing.MAX_SECTION_CHARS + 20)

    def test_missing_directory_does_not_invent_content(self):
        now = datetime(2026, 11, 10, 8, 30, tzinfo=TZ)
        text = briefing.compose_morning_briefing(
            ashare("2026-11-10"), now, self.root / "absent"
        )["text"]
        self.assertIn("缺报/未生成", text)
        self.assertIn("早报 2026-11-10", text)


if __name__ == "__main__":
    unittest.main()
