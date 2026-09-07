#!/usr/bin/env python3
"""Daily digest: merge inbox files + DB stats into out/daily-YYYY-MM-DD.md; optional Telegram."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(os.environ.get("SIGNAL_GATE_HOME", "/opt/signal-gate"))
DB_PATH = Path(os.environ.get("SIGNAL_GATE_DB", str(BASE / "data" / "signals.db")))
SECRETS_PATH = Path(os.environ.get("SIGNAL_GATE_SECRETS", str(BASE / "secrets.env")))
INBOX = BASE / "inbox"
OUT = BASE / "out"
TZ = ZoneInfo("Asia/Shanghai")
SOURCES = ("jd", "nng8", "quant")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("signal-gate-digest")


def load_secrets(path: Path) -> dict[str, str]:
    secrets: dict[str, str] = {}
    if not path.is_file():
        return secrets
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        secrets[key.strip()] = value.strip().strip('"').strip("'")
    return secrets


def db_stats(db_path: Path) -> dict[str, int]:
    stats = {s: 0 for s in SOURCES}
    stats["total"] = 0
    if not db_path.is_file():
        return stats
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT source, COUNT(*) FROM pending_signals GROUP BY source"
            ).fetchall()
            for source, count in rows:
                stats[str(source)] = int(count)
                stats["total"] += int(count)
            day_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            day_ts = int(day_start.timestamp())
            today = conn.execute(
                "SELECT COUNT(*) FROM pending_signals WHERE received_at >= ?",
                (day_ts,),
            ).fetchone()[0]
            stats["today"] = int(today)
    except sqlite3.Error as exc:
        log.warning("db stats failed: %s", exc)
        stats["today"] = 0
    return stats


def collect_inbox(source: str) -> list[Path]:
    d = INBOX / source
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.md")) + sorted(d.glob("*.json"))
    return files


def render_digest(day: str, stats: dict[str, int]) -> str:
    lines: list[str] = []
    lines.append(f"# Signal Gate Daily Digest — {day}")
    lines.append("")
    lines.append(f"Generated at: {datetime.now(TZ).isoformat()}")
    lines.append("")
    lines.append("## Review stats (SQLite)")
    lines.append("")
    lines.append(f"- total pending rows: {stats.get('total', 0)}")
    lines.append(f"- received today (Asia/Shanghai): {stats.get('today', 0)}")
    for s in SOURCES:
        lines.append(f"- source `{s}` lifetime rows: {stats.get(s, 0)}")
    lines.append("")
    lines.append("## Inbox consolidation")
    lines.append("")
    for source in SOURCES:
        files = collect_inbox(source)
        lines.append(f"### {source}")
        lines.append("")
        if not files:
            lines.append("**缺报** — no inbox artifacts present.")
            lines.append("")
            continue
        lines.append(f"Found {len(files)} file(s):")
        lines.append("")
        for fp in files[:50]:
            lines.append(f"- `{fp.name}`")
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                lines.append(f"  (read error: {exc})")
                continue
            if fp.suffix == ".json":
                try:
                    obj = json.loads(text)
                    summary = {
                        "idempotency_key": obj.get("idempotency_key"),
                        "signal_type": obj.get("signal_type"),
                        "symbol": obj.get("symbol"),
                        "confidence": obj.get("confidence"),
                        "review_status": obj.get("review_status"),
                    }
                    lines.append("```json")
                    lines.append(json.dumps(summary, ensure_ascii=False, indent=2))
                    lines.append("```")
                except json.JSONDecodeError:
                    lines.append("```")
                    lines.append(text[:800])
                    lines.append("```")
            else:
                lines.append("```markdown")
                lines.append(text[:2000])
                lines.append("```")
            lines.append("")
        if len(files) > 50:
            lines.append(f"... and {len(files) - 50} more")
            lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("This is a consolidated stub report for the four-VPS A-share pipeline.")
    lines.append("Public nginx high-entropy proxy is not enabled yet; service is localhost-only.")
    lines.append("")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    # Telegram message limit ~4096; truncate safely
    body = text if len(text) <= 3900 else text[:3900] + "\n\n…(truncated)"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {"chat_id": chat_id, "text": body, "disable_web_page_preview": True}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            log.info("telegram send status=%s bytes=%s", resp.status, len(raw))
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        log.error("telegram HTTPError %s: %s", exc.code, exc.read()[:300])
        return False
    except Exception as exc:  # noqa: BLE001
        log.error("telegram send failed: %s", exc)
        return False


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    stats = db_stats(DB_PATH)
    report = render_digest(day, stats)
    out_path = OUT / f"daily-{day}.md"
    out_path.write_text(report, encoding="utf-8")
    log.info("wrote digest %s (%s bytes)", out_path, out_path.stat().st_size)

    secrets = load_secrets(SECRETS_PATH)
    token = secrets.get("TELEGRAM_DIGEST_BOT_TOKEN", "").strip()
    chat_id = secrets.get("TELEGRAM_DIGEST_CHAT_ID", "").strip()
    if token and chat_id:
        ok = send_telegram(token, chat_id, report)
        log.info("telegram digest %s", "sent" if ok else "FAILED")
    else:
        missing = []
        if not token:
            missing.append("TELEGRAM_DIGEST_BOT_TOKEN")
        if not chat_id:
            missing.append("TELEGRAM_DIGEST_CHAT_ID")
        log.info("telegram digest skipped (missing: %s)", ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
