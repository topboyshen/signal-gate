#!/usr/bin/env python3
"""Daily digest: plain-language Chinese total report for Telegram."""
from __future__ import annotations

import json
import logging
import os
import re
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

NOISE_KEY = re.compile(
    r"(selftest|review-selftest|e2e-forward|nginx-probe|curl-valid|probe:|test:|dedirock:probe)",
    re.I,
)

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
        k, _, v = line.partition("=")
        secrets[k.strip()] = v.strip().strip('"').strip("'")
    return secrets


def is_noise_key(key: str | None) -> bool:
    if not key:
        return False
    k = key.lower()
    prefixes = (
        "selftest",
        "review-selftest",
        "e2e-forward",
        "nginx-probe",
        "curl-valid",
        "dedirock:probe",
        "test:",
        "research_eod:nng8:probe",
    )
    if k.startswith(prefixes) or ":probe:" in k or k.startswith("probe"):
        # allow real keys that merely contain the letters "test" (e.g. reviewtest)
        if k.startswith("test:"):
            return True
        if any(k.startswith(p) for p in prefixes if p != "test:"):
            return True
        if ":probe:" in k or k.startswith("probe"):
            return True
        return False
    return False


def side_cn(side: object) -> str:
    if side in (None, "", "null"):
        return "方向不明"
    s = str(side).lower()
    if s in ("long", "buy", "多"):
        return "偏多"
    if s in ("short", "sell", "空"):
        return "偏空"
    return str(side)


def load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def review_section(db_path: Path) -> list[str]:
    lines = ["【人审结论】"]
    if not db_path.is_file():
        lines.append("缺报：尚无人审数据库。")
        return lines
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = list(
            conn.execute(
                """
                SELECT idempotency_key, symbol, signal_type, review_status, decision,
                       reviewer, quant_status, confidence, body_json
                FROM pending_signals
                ORDER BY id DESC
                """
            )
        )
    real = [r for r in rows if not is_noise_key(r["idempotency_key"])]
    pending = [r for r in real if (r["review_status"] or "pending") == "pending"]
    approved = [r for r in real if r["decision"] == "approve"]
    rejected = [r for r in real if r["decision"] == "reject"]
    lines.append(
        f"待审 {len(pending)} 条 · 通过 {len(approved)} 条 · 拒绝 {len(rejected)} 条"
        f"（已过滤联调探针）"
    )
    if not real:
        lines.append("今天还没有正式待审单。")
        return lines
    # show up to 5 latest real items
    for r in real[:5]:
        status = r["decision"] or r["review_status"] or "pending"
        status_cn = {
            "approve": "通过",
            "reject": "拒绝",
            "pending": "待审",
            "reviewed": "已审",
        }.get(str(status), str(status))
        quant = r["quant_status"] or "-"
        conf = r["confidence"]
        conf_s = f"{float(conf):.2f}" if conf is not None else "-"
        side_val = None
        try:
            body = json.loads(r["body_json"] or "{}")
            if isinstance(body, dict):
                side_val = body.get("side")
        except (TypeError, json.JSONDecodeError):
            side_val = None
        lines.append(
            f"- {r['symbol']}｜{status_cn}｜{side_cn(side_val)}"
            f"｜置信 {conf_s}｜Quant {quant}"
        )
    if len(real) > 5:
        lines.append(f"…另有 {len(real) - 5} 条未展开")
    return lines


def research_section() -> list[str]:
    lines = ["【研究要点】"]
    md = read_text(INBOX / "nng8" / "digest-latest.md")
    data = load_json(INBOX / "nng8" / "digest-latest.json")
    if not md and not data:
        lines.append("缺报：研究摘要未送达。")
        return lines

    # Prefer explicit plain text fields from nnG8
    if isinstance(data, dict):
        for key in ("markdown", "text", "summary", "plain"):
            val = data.get(key)
            if isinstance(val, str) and val.strip() and len(val.strip()) > 10:
                kept = []
                for line in val.splitlines():
                    s = line.strip()
                    if not s or s.startswith("```"):
                        continue
                    kept.append(s)
                    if len(kept) >= 8:
                        break
                lines.extend(kept)
                return lines
        signals = data.get("signals")
        if isinstance(signals, list) and signals:
            lines.append(f"信号 {len(signals)} 条")
            for item in signals[:5]:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    f"- {item.get('symbol','?')}｜{side_cn(item.get('side'))}"
                    f"｜置信 {item.get('confidence','-')}"
                )
            return lines
        # watchlist / observations style
        for key in ("watchlist", "observations", "symbols", "items"):
            arr = data.get(key)
            if isinstance(arr, list) and arr:
                lines.append(f"观察 {len(arr)} 项")
                for item in arr[:5]:
                    if isinstance(item, dict):
                        sym = item.get("symbol") or item.get("code") or "?"
                        note = item.get("note") or item.get("reason") or item.get("title") or ""
                        note = str(note).replace("\n", " ").strip()
                        if len(note) > 36:
                            note = note[:36] + "…"
                        lines.append(f"- {sym}｜{side_cn(item.get('side'))}" + (f"｜{note}" if note else ""))
                    else:
                        lines.append(f"- {item}")
                return lines

    if md:
        blob = md.lower()
        if ("skeleton" in blob or "not wired" in blob) and "观察" not in md:
            lines.append("今日无有效研究信号（实盘 research_eod 尚未接入，仅健康态）。")
            return lines
        kept = []
        for line in md.splitlines():
            s = line.strip()
            if not s or s.startswith("```") or s.startswith("{"):
                continue
            if any(x in s.lower() for x in ("not wired", "skeleton", "digest skeleton")):
                continue
            kept.append(s)
            if len(kept) >= 8:
                break
        if kept:
            lines.extend(kept)
            return lines

    lines.append("研究摘要已到，但未能解析出可读要点。")
    return lines


def news_section() -> list[str]:
    lines = ["【资讯要点】"]
    md = read_text(INBOX / "jd" / "digest-latest.md")
    data = load_json(INBOX / "jd" / "digest-latest.json")
    if not md and not data:
        lines.append("缺报：资讯摘要未送达。")
        return lines

    items: list[dict] = []
    if data:
        raw_items = data.get("items") or data.get("events") or data.get("top") or []
        if isinstance(raw_items, list):
            for it in raw_items:
                if isinstance(it, dict):
                    items.append(it)

    # Deduplicate by symbol, prefer higher confidence, skip pure index spam somewhat
    best: dict[str, dict] = {}
    for it in items:
        sym = str(it.get("symbol") or it.get("code") or "").strip()
        if not sym:
            continue
        # soft-filter common index codes if many — still allow but rank lower later
        conf = it.get("confidence")
        try:
            conf_f = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            conf_f = 0.0
        prev = best.get(sym)
        if prev is None or conf_f >= float(prev.get("_conf", 0)):
            it = dict(it)
            it["_conf"] = conf_f
            best[sym] = it

    ranked = sorted(best.values(), key=lambda x: x.get("_conf", 0), reverse=True)
    # Prefer non-399/000688-looking? Keep all but limit 6
    if ranked:
        lines.append(f"高置信约 {len(ranked)} 只标的（去重后）")
        for it in ranked[:6]:
            title = it.get("title") or it.get("reason") or it.get("summary") or "—"
            title = str(title).replace("\n", " ").strip()
            if len(title) > 40:
                title = title[:40] + "…"
            lines.append(
                f"- {it.get('symbol')}｜{side_cn(it.get('side'))}"
                f"｜置信 {it.get('_conf', 0):.2f}｜{title}"
            )
    elif md:
        # parse simple bullet lines from md, skip noise
        bullets = []
        header = None
        for line in md.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("[JD") or s.startswith("资讯"):
                header = s
                continue
            if s.startswith("-"):
                # drop truncated evt ids heavy lines a bit — keep symbol-ish
                if "selftest" in s.lower() or "probe" in s.lower():
                    continue
                bullets.append(s.lstrip("- "))
        if header:
            # rewrite header into plain
            m = re.search(r"items=(\d+)", header)
            n = m.group(1) if m else str(len(bullets))
            lines.append(f"高置信资讯约 {n} 条")
        for b in bullets[:6]:
            # transform "399006.SZ side=long conf=0.99 evt=..." 
            mm = re.match(
                r"(?P<sym>[0-9]{6}\.(?:SH|SZ))\s+side=(?P<side>\S+)\s+conf=(?P<conf>\S+)",
                b,
            )
            if mm:
                lines.append(
                    f"- {mm.group('sym')}｜{side_cn(None if mm.group('side') in ('None','null') else mm.group('side'))}"
                    f"｜置信 {mm.group('conf')}"
                )
            else:
                lines.append(f"- {b[:60]}")
    else:
        lines.append("有摘要文件，但未能解析出个股要点。")
    return lines


def execution_section() -> list[str]:
    lines = ["【执行摘要】"]
    data = load_json(INBOX / "quant" / "digest-latest.json")
    if not data:
        lines.append("缺报：执行摘要未送达。")
        return lines
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    accepted = summary.get("accepted", data.get("accepted"))
    rejected = summary.get("rejected", data.get("rejected"))
    fills = summary.get("fills", data.get("fills"))
    pnl = summary.get("pnl", data.get("pnl"))
    note = summary.get("note") or data.get("note") or ""

    # Count non-noise items if present
    items = data.get("items") if isinstance(data.get("items"), list) else []
    real_items = [
        it
        for it in items
        if isinstance(it, dict) and not is_noise_key(str(it.get("idempotency_key") or ""))
    ]
    noise_n = len(items) - len(real_items)

    lines.append(
        f"受理 {accepted if accepted is not None else '-'} 条 · "
        f"拒绝 {rejected if rejected is not None else '-'} 条"
    )
    if fills in (None, 0) or "stub" in str(note).lower() or "未启用" in str(note):
        lines.append("成交与盈亏：未启用（不接实盘）")
    else:
        lines.append(f"成交 {fills} · 盈亏 {pnl}")
    if noise_n > 0:
        lines.append(f"（原始受理里含 {noise_n} 条联调探针，阅读时可忽略）")
    if real_items:
        lines.append("正式相关：")
        for it in real_items[:5]:
            lines.append(
                f"- {it.get('symbol','?')}｜{it.get('status','?')}｜{side_cn(it.get('side'))}"
            )
    return lines


def render_digest(day: str) -> str:
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    parts: list[str] = [
        f"A股联动日报 {day}",
        f"生成时间 {now}（北京时间）",
        "",
    ]
    for section in (
        review_section(DB_PATH),
        research_section(),
        news_section(),
        execution_section(),
    ):
        parts.extend(section)
        parts.append("")
    parts.append("说明：只保留可读摘要；原始 JSON / 探针已隐藏。")
    return "\n".join(parts).strip() + "\n"


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            log.info("telegram send status=%s bytes=%s", resp.status, len(raw))
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError) as exc:
        log.error("telegram send failed: %s", exc)
        return False


def main() -> int:
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    OUT.mkdir(parents=True, exist_ok=True)
    report = render_digest(day)
    out_path = OUT / f"daily-{day}.md"
    out_path.write_text(report, encoding="utf-8")
    log.info("wrote digest %s (%s bytes)", out_path, len(report.encode()))

    secrets = load_secrets(SECRETS_PATH)
    token = secrets.get("TELEGRAM_DIGEST_BOT_TOKEN", "").strip()
    chat_id = secrets.get("TELEGRAM_DIGEST_CHAT_ID", "").strip()
    if token and chat_id:
        ok = send_telegram(token, chat_id, report)
        if ok:
            log.info("telegram digest sent")
            return 0
        log.error("telegram digest failed")
        return 1
    missing = []
    if not token:
        missing.append("TELEGRAM_DIGEST_BOT_TOKEN")
    if not chat_id:
        missing.append("TELEGRAM_DIGEST_CHAT_ID")
    log.warning("telegram skipped missing=%s", ",".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
