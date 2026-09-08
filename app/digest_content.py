"""Bounded, read-only rendering of untrusted Signal Gate digest inputs.

No network, credential loading, SQLite migrations, or output writes belong here.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
MAX_INPUT = 512 * 1024
MAX_DB = 64 * 1024 * 1024
MAX_ROWS = 1000
MAX_BODY = 16 * 1024
MAX_TEXT = 140
DATE_KEYS = ("as_of", "report_date", "date", "generated_at", "updated_at", "timestamp")
LABELS = {"nng8": "研究", "jd": "资讯", "quant": "执行", "review": "人审"}
WARNINGS = {
    "missing": "缺报", "stale": "超过72小时", "unknown_date": "来源日期未知",
    "future": "来源日期在未来", "invalid": "内容无效", "schema_error": "字段不兼容",
    "too_large": "输入超限", "truncated": "仅展示受限范围", "io_error": "读取失败",
    "expiry_unknown": "待审有效期不明", "placeholder": "仅占位内容",
}
URL_RE = re.compile(r"(?:https?://|ftp://|file://|tg://)[^\s<>\[\]（）\"']+", re.I)
RAW_CREDENTIAL_RE = re.compile(
    r"(?<![\w-])(?:sk[-_][A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}"
    r"|xox[baprs]-[A-Za-z0-9-]{16,}|AIza[A-Za-z0-9_-]{25,}"
    r"|AKIA[A-Z0-9]{16}|[0-9]{6,12}:[A-Za-z0-9_-]{30,}"
    r"|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,})(?![\w-])"
)
NOISE_PREFIXES = (
    "selftest", "review-selftest", "e2e-forward", "nginx-probe", "curl-valid",
    "dedirock:probe", "test:", "research_eod:nng8:probe", "probe",
)


def safe_url(value: object) -> str:
    """Conservative public links only; never forward bearer/private URLs."""
    if not isinstance(value, str) or len(value) > 300:
        return ""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if (parts.scheme not in ("http", "https") or not host or parts.username
                or parts.password or parts.query or parts.fragment or parts.port not in (None, 80, 443)):
            return ""
        if host.lower() == "localhost" or "." not in host or host.endswith((".local", ".internal", ".lan")):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            if re.fullmatch(r"[0-9.]+", host):
                return ""
        path = parts.path
        for _ in range(3):
            path = unquote(path)
        if re.search(r"(?i)(token|secret|password|signature|auth|invite|subscribe|subscription|/bot[0-9])", path):
            return ""
        if any(len(piece) > 48 or re.fullmatch(r"[A-Za-z0-9]{24,}", piece)
               or re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", piece)
               or len(piece) >= 24 and re.fullmatch(r"[A-Za-z0-9_-]+", piece) and re.search(r"[0-9]", piece) and re.search(r"[A-Za-z]", piece)
               for piece in path.split("/")):
            return ""
        if RAW_CREDENTIAL_RE.search(value) or any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in value):
            return ""
        return value
    except (ValueError, UnicodeError):
        return ""


def clean(value: object, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = str(value)[:MAX_BODY]
    text = URL_RE.sub(lambda m: safe_url(m.group(0)) or "[私密或无效链接已隐藏]", text)
    text = RAW_CREDENTIAL_RE.sub("[凭据已隐藏]", text)
    text = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*", "[私钥已隐藏]", text, flags=re.S)
    text = re.sub(r"(?i)\b(?:bearer\s+\S+|(?:token|secret|password|api[_-]?key)\s*[:=]\s*\S+)", "[敏感字段已隐藏]", text)
    text = " ".join("".join(ch if not unicodedata.category(ch).startswith("C") else " " for ch in text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def parse_date(value: object, *, epoch: bool = False) -> datetime | None:
    try:
        if epoch and isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, TZ)
        if not isinstance(value, str) or len(value) > 64:
            return None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})?)?", value):
            return None
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)
    except (ValueError, OverflowError, OSError):
        return None


def is_noise(item: dict) -> bool:
    key = str(item.get("idempotency_key") or "").lower()
    if key.startswith(NOISE_PREFIXES) or ":probe:" in key:
        return True
    for name in ("simulation", "is_simulation", "simulated", "is_test", "test", "dry_run"):
        value = item.get(name)
        if value is True or value == 1 or isinstance(value, str) and value.lower() in ("true", "yes", "1", "simulation", "simulated"):
            return True
    return any(str(item.get(name) or "").lower() in ("test", "simulation", "simulated", "paper", "sandbox", "demo", "dry_run") for name in ("mode", "environment", "origin"))


def side(value: object) -> str:
    return {"long": "偏多", "buy": "偏多", "多": "偏多", "short": "偏空", "sell": "偏空", "空": "偏空"}.get(str(value).lower(), "方向未知")


class Renderer:
    def __init__(self, base: Path, db_path: Path, now: datetime):
        self.base, self.db_path, self.now = base.resolve(), db_path, now
        self.issues: list[str] = []
        self.sources: dict[str, dict] = {}
        self.pending_count: int | None = None

    def issue(self, source: str, code: str):
        key = f"{source}:{code}"
        if key not in self.issues:
            self.issues.append(key)

    def read(self, path: Path, source: str) -> str | None:
        try:
            if not path.resolve().is_relative_to(self.base) or path.is_symlink():
                self.issue(source, "invalid")
                return None
            if not path.is_file():
                return None
            with path.open("rb") as stream:
                raw = stream.read(MAX_INPUT + 1)
            if len(raw) > MAX_INPUT:
                self.issue(source, "too_large")
                return None
            return raw.decode("utf-8")
        except UnicodeError:
            self.issue(source, "invalid")
        except OSError:
            self.issue(source, "io_error")
        return None

    def source(self, name: str) -> tuple[dict | None, str]:
        folder = self.base / "inbox" / name
        raw = self.read(folder / "digest-latest.json", name)
        md = self.read(folder / "digest-latest.md", name) if name != "quant" else None
        data = None
        if raw is not None:
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    self.issue(name, "schema_error")
                    data = None
            except (ValueError, RecursionError):
                self.issue(name, "invalid")
        if raw is None and md is None:
            self.issue(name, "missing")
            self.sources[name] = {"status": "missing", "as_of": None, "age_hours": None}
            return None, ""
        stamp = None
        supplied = False
        if data is not None:
            for key in DATE_KEYS:
                if key in data:
                    supplied, stamp = True, parse_date(data[key])
                    break
        if not supplied and md:
            match = re.search(r"(?im)^\s*(?:as_of|date|日期|数据日期|来源日期|生成时间)\s*[:：=]\s*(\S+(?: \d\d:\d\d(?::\d\d)?)?)", "\n".join(md.splitlines()[:12]))
            if match:
                supplied, stamp = True, parse_date(match.group(1))
        status = "ok"
        if stamp is None:
            status = "invalid" if supplied else "unknown_date"
        elif stamp > self.now:
            status = "future"
        elif self.now - stamp > timedelta(hours=72):
            status = "stale"
        if status != "ok":
            self.issue(name, status)
        self.sources[name] = {
            "status": status, "as_of": stamp.isoformat() if stamp else None,
            "age_hours": round((self.now - stamp).total_seconds() / 3600, 1) if stamp else None,
        }
        return data, md or ""

    def highlights(self, name: str) -> list[str]:
        data, md = self.source(name)
        if data is None and not md:
            return [f"{LABELS[name]}：未知（缺报或内容无效）。"]
        if data and is_noise(data):
            return [f"{LABELS[name]}：所报内容仅模拟/探针，已排除。"]
        arrays = ("signals", "watchlist", "observations", "symbols", "items") if name == "nng8" else ("items", "events", "top")
        items = None
        for key in arrays:
            if data is not None and key in data:
                if not isinstance(data[key], list):
                    self.issue(name, "schema_error")
                else:
                    items = data[key]
                break
        if items is not None:
            kept, filtered, invalid_items = [], 0, 0
            for item in items[:MAX_ROWS]:
                if isinstance(item, str):
                    item = {"title": item}
                if not isinstance(item, dict):
                    self.issue(name, "schema_error")
                    invalid_items += 1
                    continue
                if is_noise(item):
                    filtered += 1
                    continue
                title = item.get("title") or item.get("note") or item.get("reason") or item.get("summary")
                symbol = clean(item.get("symbol") or item.get("code"), 24)
                title = clean(title)
                if not symbol and not title:
                    self.issue(name, "schema_error")
                    invalid_items += 1
                    continue
                line = "｜".join(part for part in (symbol, side(item.get("side")) if symbol else "", title) if part)
                date_key = next((key for key in ("published_at", "timestamp", "as_of", "date") if key in item), None)
                stamp = parse_date(item[date_key]) if date_key else None
                if date_key and stamp is None:
                    self.issue(name, "invalid")
                if stamp:
                    line += f"（{stamp.strftime('%Y-%m-%d %H:%M')}）"
                    if stamp > self.now:
                        self.issue(name, "future")
                    elif self.now - stamp > timedelta(hours=72):
                        self.issue(name, "stale")
                link = safe_url(item.get("url") or item.get("link") or item.get("source_url"))
                if link:
                    line += f" {link}"
                if line not in kept:
                    kept.append(line)
            if len(items) > MAX_ROWS:
                self.issue(name, "truncated")
            if not kept:
                if invalid_items:
                    return [f"{LABELS[name]}：未知（所报条目无法解析）。"]
                return [f"{LABELS[name]}：本次上报0条可展示内容" + (f"（排除模拟/探针{filtered}条）。" if filtered else "。")]
            return [self.highlight_heading(name)] + [f"- {line}" for line in kept[:3]]
        text = next((data[key] for key in ("markdown", "text", "summary", "plain") if isinstance(data, dict) and isinstance(data.get(key), str) and data[key].strip()), md)
        if text and any(word in text.lower() for word in ("skeleton", "not wired")):
            self.issue(name, "placeholder")
            return [f"{LABELS[name]}：仅占位摘要，尚无有效要点。"]
        lines = []
        for raw_line in text.splitlines()[:MAX_ROWS]:
            value = clean(raw_line.strip().lstrip("#*- "))
            if is_noise({"idempotency_key": value}) or re.search(r"(?i)(selftest|e2e-forward|nginx-probe|dedirock:probe|\bprobe[:=])", value):
                continue
            if value and not raw_line.strip().startswith(("```", "{")) and not re.match(r"(?i)^(as_of|date|日期|数据日期|来源日期|生成时间)\s*[:：=]", value):
                lines.append(f"- {value}")
            if len(lines) == 3:
                break
        if not lines:
            self.issue(name, "schema_error")
        return [self.highlight_heading(name)] + (lines or ["未知：未解析出有效要点。"])

    def highlight_heading(self, name: str) -> str:
        if f"{name}:stale" in self.issues:
            scope = "含过期历史内容，不作新增"
        elif any(f"{name}:{code}" in self.issues for code in ("future", "unknown_date", "invalid")):
            scope = "日期未知或异常，不作新增"
        else:
            scope = "本次上报，非今日新增计数"
        return f"{LABELS[name]}（{scope}）："

    def reviews(self) -> list[str]:
        lines = ["【待审与既往人审】"]
        path = self.db_path
        try:
            if not path.resolve().is_relative_to(self.base) or path.is_symlink():
                self.issue("review", "invalid")
                raise ValueError
            if not path.is_file():
                self.issue("review", "missing")
                raise ValueError
            if path.stat().st_size > MAX_DB:
                self.issue("review", "too_large")
                raise ValueError
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                cols = {row[1] for row in conn.execute("PRAGMA table_info(pending_signals)")}
                wanted = ("id", "idempotency_key", "symbol", "review_status", "decision", "quant_status", "expires_at", "body_json", "received_at", "reviewed_at")
                selected = [key for key in wanted if key in cols]
                # Missing state/decision fields must never masquerade as zero.
                required = {"idempotency_key", "review_status", "decision", "expires_at", "body_json"}
                if not required.issubset(cols):
                    self.issue("review", "schema_error")
                    raise ValueError
                if not set(wanted[1:]).issubset(cols):
                    self.issue("review", "schema_error")
                projection = ", ".join(f'substr("{key}", 1, {MAX_BODY + 1}) AS "{key}"' if key == "body_json" else f'"{key}"' for key in selected)
                order = ' ORDER BY "id" DESC' if "id" in cols else ""
                rows = [dict(row) for row in conn.execute(f"SELECT {projection} FROM pending_signals{order} LIMIT {MAX_ROWS + 1}")]
        except (OSError, sqlite3.Error):
            self.issue("review", "io_error")
            rows = None
        except ValueError:
            rows = None
        if rows is None:
            self.sources["review"] = {"status": "unavailable", "as_of": None, "age_hours": None}
            return lines + ["待审、今日新增及累计人审：未知（数据库不可用）。"]
        limited = len(rows) > MAX_ROWS
        if limited:
            self.issue("review", "truncated")
        valid, expired, unknown, filtered, approved, rejected = [], 0, 0, 0, 0, 0
        unknown_status = 0
        received, reviewed, stamps = [], [], []
        review_results = []
        for row in rows[:MAX_ROWS]:
            body = {}
            raw_body = row.get("body_json")
            if isinstance(raw_body, str) and len(raw_body) <= MAX_BODY:
                try:
                    body = json.loads(raw_body)
                    if not isinstance(body, dict):
                        body = {}
                        self.issue("review", "invalid")
                except (ValueError, RecursionError):
                    self.issue("review", "invalid")
            elif raw_body is not None:
                self.issue("review", "invalid")
            if is_noise(row) or is_noise(body):
                filtered += 1
                continue
            merged = {**body, **{key: value for key, value in row.items() if value is not None}}
            receipt = parse_date(merged.get("received_at"), epoch=True)
            received.append(receipt if receipt and receipt <= self.now else None)
            decision = merged.get("decision")
            if decision not in (None, "", "approve", "reject"):
                self.issue("review", "schema_error")
                unknown_status += 1
                continue
            review_time = parse_date(merged.get("reviewed_at"), epoch=True)
            if decision in ("approve", "reject"):
                reviewed.append(review_time if review_time and review_time <= self.now else None)
            stamps.extend(stamp for stamp in (receipt, review_time) if stamp and stamp <= self.now)
            if receipt and receipt > self.now or review_time and review_time > self.now:
                self.issue("review", "future")
            approved += decision == "approve"
            rejected += decision == "reject"
            if decision in ("approve", "reject") and len(review_results) < 3:
                state = "通过" if decision == "approve" else "拒绝"
                quant = merged.get("quant_status")
                delivered = "已提交/受理，非成交" if quant in ("ok", "accepted", "submitted") else "投递失败" if quant in ("failed", "error") else "未提交（已过期）" if quant == "skipped_expired" else "执行结果未知"
                review_results.append(f"- {clean(merged.get('symbol'), 24) or '标的未知'}｜{state}｜{delivered}")
            if merged.get("review_status") == "pending" and decision not in ("approve", "reject"):
                expiry = parse_date(merged.get("expires_at"))
                if expiry is None:
                    unknown += 1
                    self.issue("review", "expiry_unknown")
                elif expiry <= self.now:
                    expired += 1
                else:
                    valid.append((merged, expiry))
            elif merged.get("review_status") not in ("pending", "reviewed") and decision not in ("approve", "reject"):
                unknown_status += 1
                self.issue("review", "schema_error")
        if any(stamp is None for stamp in received + reviewed):
            self.issue("review", "unknown_date")
        self.pending_count = len(valid) if not limited and not unknown_status else None
        scope = f"最近{MAX_ROWS}条范围" if limited else "全部本地留存"
        today_received = "未知" if any(stamp is None for stamp in received) else str(sum(stamp.date() == self.now.date() for stamp in received))
        today_reviewed = "未知" if any(stamp is None for stamp in reviewed) else str(sum(stamp.date() == self.now.date() for stamp in reviewed))
        prefix = f"最近{MAX_ROWS}条中，" if limited else ""
        pending_line = f"{prefix}当前有效待审 {len(valid)} 条；已过期 {expired} 条不计入；有效期未知 {unknown} 条。"
        if unknown_status:
            pending_line = f"{prefix}已确认有效待审 {len(valid)} 条；另有 {unknown_status} 条状态不明，总量未知；已过期 {expired} 条、有效期未知 {unknown} 条。"
        lines += [pending_line, f"今日新增 {today_received} 条；今日审核 {today_reviewed} 条。", f"累计（{scope}）：通过 {approved} 条 / 拒绝 {rejected} 条；过滤模拟/探针 {filtered} 条。"]
        for item, expiry in valid[:3]:
            lines.append(f"- {clean(item.get('symbol'), 24) or '标的未知'}｜{side(item.get('side'))}｜有效至 {expiry.strftime('%Y-%m-%d %H:%M')}")
        if review_results:
            lines += ["既往审核结果（非今日新增）："] + review_results
        last = max(stamps) if stamps else None
        self.sources["review"] = {"status": "ok", "as_of": last.isoformat() if last else None, "age_hours": None, "record_count": min(len(rows), MAX_ROWS) - filtered}
        return lines

    def execution(self) -> list[str]:
        data, _ = self.source("quant")
        lines = ["【执行结果】"]
        if data is None:
            return lines + ["受理、拒绝、成交：未知（缺报或内容无效）。"]
        if is_noise(data):
            return lines + ["所报执行为模拟/探针，已排除；真实执行情况未知。"]
        if "items" in data:
            if not isinstance(data["items"], list):
                self.issue("quant", "schema_error")
                return lines + ["执行明细无效；未采用可能不一致的汇总。"]
            counts = {key: 0 for key in ("accepted", "submitted", "rejected", "filled", "unknown")}
            filtered, details = 0, []
            for item in data["items"][:MAX_ROWS]:
                if not isinstance(item, dict):
                    self.issue("quant", "schema_error")
                    counts["unknown"] += 1
                    continue
                if is_noise(item):
                    filtered += 1
                    continue
                status = item.get("status")
                key = status if status in ("accepted", "submitted", "rejected", "filled") else "unknown"
                if key == "unknown" and not isinstance(status, (str, type(None))):
                    self.issue("quant", "schema_error")
                counts[key] += 1
                if len(details) < 3:
                    label = {"accepted": "受理（非成交）", "submitted": "提交（非成交）", "rejected": "拒绝", "filled": "上报成交（未核验）", "unknown": "状态未知"}[key]
                    details.append(f"- {clean(item.get('symbol'), 24) or '标的未知'}｜{label}")
            if len(data["items"]) > MAX_ROWS:
                self.issue("quant", "truncated")
            lines.append("范围：本次所报明细（非今日新增或历史全量）；来源性质未独立核实。")
            lines.append(f"受理 {counts['accepted']} / 提交 {counts['submitted']} / 拒绝 {counts['rejected']} / 上报成交 {counts['filled']} / 状态未知 {counts['unknown']}；排除模拟/探针 {filtered} 条。")
            return lines + details + ["成交仅表示来源上报 filled；无真实持仓或盈亏核验。"]
        summary = data.get("summary", data)
        if not isinstance(summary, dict):
            self.issue("quant", "schema_error")
            return lines + ["执行汇总字段无效；执行情况未知。"]
        values = []
        for key, label in (("accepted", "受理"), ("rejected", "拒绝"), ("fills", "成交")):
            value = summary.get(key, data.get(key))
            valid = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9
            if value is not None and not valid:
                self.issue("quant", "schema_error")
            values.append(f"{label} {value if valid else '未知'}")
        return lines + ["原始汇总（未验证明细；统计范围及模拟占比未知）：", " / ".join(values), "受理不等于成交；零成交不代表未启用，未核验持仓或盈亏。"]

    def report(self) -> dict:
        review = self.reviews()
        research, news = self.highlights("nng8"), self.highlights("jd")
        execution = self.execution()
        attention = []
        if self.pending_count:
            attention.append(f"优先核对 {self.pending_count} 条仍在有效期内的待审。")
        elif self.pending_count == 0:
            attention.append("当前有效待审为0；不代表缺报来源没有新内容。")
        else:
            attention.append("当前有效待审总量未知，请先核对数据。")
        attention.append("部分来源缺报、过期或不完整，先核对末尾警示。" if self.issues else "来源日期检查通过；以下仅供核对，不代表交易指令。")
        lines = [f"A股联动日报 {self.now.date().isoformat()}", f"生成 {self.now.strftime('%H:%M')}（北京时间）", "", "【今日关注】", *attention, "", "【新研究与资讯】", *research, *news, "", *review, "", *execution, "", "【来源日期与警示】"]
        for name in ("nng8", "jd", "quant", "review"):
            info = self.sources[name]
            source_issues = [item.split(":")[1] for item in self.issues if item.startswith(name + ":")]
            if source_issues and info["status"] == "ok":
                info["status"] = source_issues[0]
            stamp = parse_date(info["as_of"])
            label = stamp.strftime("%Y-%m-%d %H:%M") if stamp else "未知"
            if name == "review":
                if info.get("record_count") == 0:
                    label = "无纳入记录"
                lines.append(f"人审：只读快照；最新记录时间 {label}。")
            else:
                lines.append(f"{LABELS[name]}：来源时间 {label}（北京时间）。")
        if self.issues:
            lines.append("警示：" + "；".join(f"{LABELS[source]}{WARNINGS[code]}" for source, code in (item.split(":") for item in self.issues)) + "。")
        else:
            lines.append("来源不超过72小时；前一自然日和周末日期按此阈值检查。")
        lines.append("说明：过滤明确模拟标记和联调探针；其余内容不自动视为实盘。无持仓推断。")
        return {"report_date": self.now.date().isoformat(), "text": "\n".join(lines) + "\n", "issues": self.issues, "degraded": bool(self.issues), "sources": self.sources}


def build_report(base: Path, db_path: Path, now: datetime | None = None) -> dict:
    now = datetime.now(TZ) if now is None else now
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    return Renderer(Path(base), Path(db_path), now).report()
