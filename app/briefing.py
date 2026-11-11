"""Compose A-share digest with overseas and VPS briefing sections.

Missing sections are labeled as 缺报/未生成. This module never invents
market or health content, never sends messages, and performs no I/O at import.
"""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import stat

MAX_SECTION_CHARS = 12000
MAX_FILE_BYTES = 256 * 1024
OVERSEAS_SKIP = "今日无新美股收盘"
MISSING = "缺报/未生成"
ANSI = re.compile(r"\x1B\[[0-9;]*[A-Za-z]")
CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def compose_morning_briefing(report: dict, now: datetime, briefing_dir: Path | None) -> dict:
    """Append overseas, optional Saturday weekly, and VPS sections to a report dict."""
    if not isinstance(report, dict) or not isinstance(report.get("text"), str):
        return report
    day = now.date().isoformat()
    weekday = now.weekday()
    ashare = _retitle_ashare(report["text"]).rstrip() + "\n"
    overseas = _overseas_text(briefing_dir, day, weekday)
    chunks = [ashare, "", "【海外晨报】", overseas]
    if weekday == 5:
        chunks += ["", "【海外周报】", _weekly_text(briefing_dir, day)]
    chunks += ["", "【VPS 健康】", _vps_text(briefing_dir, day), ""]
    return {**report, "text": "\n".join(chunks)}


def _retitle_ashare(text: str) -> str:
    if text.startswith("A股联动日报 "):
        return "早报 " + text[len("A股联动日报 "):]
    return text


def _overseas_text(briefing_dir: Path | None, day: str, weekday: int) -> str:
    if weekday in {0, 6}:
        return OVERSEAS_SKIP
    return _load_or_missing(briefing_dir, f"daily-{day}.md")


def _weekly_text(briefing_dir: Path | None, day: str) -> str:
    return _load_or_missing(briefing_dir, f"weekly-{day}.md", missing="未生成")


def _vps_text(briefing_dir: Path | None, day: str) -> str:
    return _load_or_missing(briefing_dir, f"vps-{day}.txt")


def _load_or_missing(briefing_dir: Path | None, name: str, *, missing: str = MISSING) -> str:
    body = _read_section_file(briefing_dir, name) if briefing_dir is not None else None
    if body is None or not body.strip():
        return missing
    return body


def _read_section_file(briefing_dir: Path, name: str) -> str | None:
    if Path(name).name != name or "/" in name or name in {".", ".."}:
        return None
    try:
        root = briefing_dir.resolve()
    except OSError:
        return None
    candidate = root / name
    try:
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size < 0 or info.st_size > MAX_FILE_BYTES:
            return None
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            return None
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            raw = os.read(fd, MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        return None
    if len(raw) > MAX_FILE_BYTES:
        return None
    text = _sanitize(raw.decode("utf-8", errors="replace"))
    if len(text) > MAX_SECTION_CHARS:
        text = text[:MAX_SECTION_CHARS].rstrip() + "\n（已截断）"
    return text


def _sanitize(text: str) -> str:
    text = ANSI.sub("", text)
    text = CONTROLS.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()
