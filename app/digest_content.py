"""Bounded, read-only rendering of untrusted Signal Gate digest inputs.

No network, credential loading, SQLite migrations, or output writes belong here.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import importlib.util
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo


_history_spec = importlib.util.spec_from_file_location("content_news_history", Path(__file__).with_name("news_history.py"))
assert _history_spec is not None and _history_spec.loader is not None
news_history = importlib.util.module_from_spec(_history_spec)
_history_spec.loader.exec_module(news_history)

_research_spec = importlib.util.spec_from_file_location("content_research_receipts", Path(__file__).with_name("research_receipts.py"))
assert _research_spec is not None and _research_spec.loader is not None
research_receipts = importlib.util.module_from_spec(_research_spec)
_research_spec.loader.exec_module(research_receipts)

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
SYMBOL_RE = re.compile(r"\d{6}\.(?:SH|SZ|BJ)")
INDEX_NAMES = {
    "000001.SH": "上证指数", "000016.SH": "上证50", "000300.SH": "沪深300",
    "000510.SH": "中证A500", "000688.SH": "科创50", "000852.SH": "中证1000",
    "000905.SH": "中证500", "399001.SZ": "深证成指", "399005.SZ": "中小100",
    "399006.SZ": "创业板指", "399300.SZ": "沪深300", "399673.SZ": "创业板50",
}


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


def freshness_stamp(stamp: datetime | None, source_value: object) -> datetime | None:
    """Date-only stamps count as end of that local day for the 72h freshness window.

    Midnight would falsely mark Friday trading-day digests stale on Monday morning.
    Future checks and display keep the parsed (start-of-day) stamp.
    """
    if stamp is None:
        return None
    if isinstance(source_value, str) and len(source_value) == 10:
        return stamp.replace(hour=23, minute=59, second=59, microsecond=999999)
    return stamp


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


def first_text(data: dict, fields: tuple[str, ...], limit: int = MAX_TEXT) -> str:
    """Only explicit text fields, never repr() an arbitrary nested payload."""
    for field in fields:
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return clean(value, limit)
    return ""


def research_section(value: str) -> str:
    """Recognize producer labels, not market sentiment or trading priority."""
    if re.match(r"^(?:观察(?:标的|池|清单|方向|要点)?|今日关注|重点关注|关注标的|策略)", value):
        return "watch"
    if re.match(r"^(?:板块|行业|题材)[：:]", value):
        return "sector"
    if re.match(r"^(?:大盘|市场)[：:]|^(?:成交额|成交量|涨跌家数)(?:[：:\s]|\d)", value):
        return "market"
    return ""


def research_references(value: str) -> set[str]:
    """Parse every reserved source marker; never ignore malformed/grouped refs."""
    pattern = re.compile(r"\[来源\s*([1-9][0-9]*(?:(?:\s*[、,，]\s*|\s+)[1-9][0-9]*)*)\s*\]")
    references: set[str] = set()
    for match in pattern.finditer(value):
        references.update(re.findall(r"[1-9][0-9]*", match[1]))
    if "[来源" in pattern.sub("", value):
        raise ValueError("invalid_source_reference")
    return references


def instrument_kind(symbol: str) -> str:
    if not symbol:
        return "none"
    if symbol in INDEX_NAMES:
        return "index"
    if SYMBOL_RE.fullmatch(symbol):
        return "stock"
    return "other"


def display_instrument(symbol: str, name: str = "") -> str:
    """Index → 指数名; stock with a local short name → '简称 代码'; otherwise keep the code."""
    kind = instrument_kind(symbol)
    if kind == "index":
        return INDEX_NAMES[symbol]
    label = clean(name, 12)
    if kind == "stock" and label and label != symbol and not SYMBOL_RE.search(label):
        return f"{label} {symbol}"
    return symbol or "标的未知"


def harvest_names(*texts: str) -> dict[str, str]:
    names: dict[str, str] = {}
    blob = "\n".join(texts)
    for match in re.finditer(r"(\d{6}\.(?:SH|SZ|BJ))\s+([\u4e00-\u9fffA-Za-z0-9·]{2,10})", blob):
        code, name = match.group(1), match.group(2)
        if code in INDEX_NAMES or name in INDEX_NAMES.values():
            continue
        if name in {"方向未知", "偏多", "偏空", "通过", "拒绝"}:
            continue
        names.setdefault(code, name)
    for match in re.finditer(r"([\u4e00-\u9fff]{2,8})\s+(\d{6}\.(?:SH|SZ|BJ))", blob):
        name, code = match.group(1), match.group(2)
        if code in INDEX_NAMES or name in INDEX_NAMES.values():
            continue
        names.setdefault(code, name)
    return names


def decorate_symbols(line: str, names: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        return display_instrument(match.group(0), names.get(match.group(0), ""))
    return SYMBOL_RE.sub(repl, line)


def news_item_key(item: dict, provenance: dict) -> str:
    """Conservative, batch-local identity; never infer equivalent financial facts.

    Use full input fields, not redacted/truncated display text. Only whitespace
    in editorial text is normalized: numbers, signs, units, dates, entities and
    corrections remain significant. Differing provenance is kept, even when
    the rendered headlines look identical. This internal key must not be logged
    or persisted; input size is bounded by the reader.
    """
    text_fields = ("title", "note", "reason", "summary")
    fields = text_fields + (
        "symbol", "code", "name", "short_name", "sec_name", "side", "source",
        "url", "link", "source_url", "event_id", "idempotency_key",
        "published_at", "timestamp", "as_of", "date",
    )
    layers = []
    for mapping in (item, provenance):
        identity = {}
        for field in fields:
            if field in mapping:
                value = mapping[field]
                identity[field] = " ".join(value.split()) if field in text_fields and isinstance(value, str) else value
        layers.append(identity)
    return json.dumps(layers, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Renderer:
    def __init__(self, base: Path, db_path: Path, now: datetime, *, require_sealed_research: bool = True,
                 news_snapshot: tuple[str, dict] | None = None):
        self.base, self.db_path, self.now = base.resolve(), db_path, now
        self.require_sealed_research = require_sealed_research
        self.news_snapshot = news_snapshot  # Only the verified news-history CLI supplies this.
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
        except (OSError, RuntimeError):
            self.issue(source, "io_error")
        return None

    def news_receipt(self, raw: str | None) -> dict:
        if self.news_snapshot is not None:
            return {**self.news_snapshot[1], "receipt_status": "verified"}
        if raw is not None:
            try:
                return {**news_history.latest_receipt(self.base, raw), "receipt_status": "verified"}
            except news_history.HistoryError as exc:
                if str(exc) != "receipt_missing":
                    self.issue("jd", "invalid")
                    return {"received_at": None, "receipt_status": "invalid"}
        return {"received_at": None, "receipt_status": "missing"}

    def source(self, name: str) -> tuple[dict | None, str]:
        folder = self.base / "inbox" / name
        snapshot = self.news_snapshot if name == "jd" else None
        research_record = None
        if name == "nng8":
            try:
                record_raw, research_record = research_receipts.read_record(
                    self.base, name, self.now.date().isoformat())
                if len(record_raw) > MAX_INPUT:
                    raise ValueError("research_input_too_large")
                received = parse_date(research_record["stored_at"])
                if received is None or received > self.now:
                    raise ValueError("research_receipt_future")
                snapshot = (record_raw.decode("utf-8"), research_record)
            except FileNotFoundError:
                pass  # Legacy fallback remains unverified; no receipt is manufactured.
            except (OSError, ValueError, RecursionError):
                self.issue(name, "invalid")
                self.sources[name] = {"status": "invalid", "as_of": None,
                                      "age_hours": None, "receipt_status": "invalid"}
                return None, ""  # Corruption cannot authorize a healthy legacy fallback.
        json_file = folder / "digest-latest.json"
        try:
            json_present = name == "jd" and (snapshot is not None or json_file.exists() or json_file.is_symlink())
        except OSError:
            json_present = True  # Unreadable JSON cannot authorize an old MD fallback.
        raw = snapshot[0] if snapshot is not None else self.read(json_file, name)
        if snapshot is not None and raw is not None and len(raw.encode("utf-8")) > MAX_INPUT:
            self.issue(name, "too_large")
            raw = None
        # Sealed research and received JSON news cannot borrow unrelated MD.
        read_markdown = (name != "quant" and not (name == "nng8" and self.require_sealed_research)
                         and not (name == "jd" and (json_present or raw is not None)))
        md = self.read(folder / "digest-latest.md", name) if read_markdown else None
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
        source_value = None
        if data is not None:
            for key in DATE_KEYS:
                if key in data:
                    source_value = data[key]
                    supplied, stamp = True, parse_date(source_value)
                    break
        if not supplied and md:
            match = re.search(r"(?im)^\s*(?:as_of|date|日期|数据日期|来源日期|生成时间)\s*[:：=]\s*(\S+(?: \d\d:\d\d(?::\d\d)?)?)", "\n".join(md.splitlines()[:12]))
            if match:
                source_value = match.group(1)
                supplied, stamp = True, parse_date(source_value)
        if name == "jd" and isinstance(source_value, str) and len(source_value) > 10:
            try:
                stamp = news_history.precise_time(source_value).astimezone(TZ)
            except news_history.HistoryError:
                stamp = None
        status = "ok"
        if stamp is None:
            status = "invalid" if supplied else "unknown_date"
        elif stamp > self.now:
            status = "future"
        else:
            age_basis = freshness_stamp(stamp, source_value)
            if age_basis is not None and self.now - age_basis > timedelta(hours=72) and not (
                    name == "nng8" and self.require_sealed_research):
                status = "stale"
        if status != "ok":
            self.issue(name, status)
        age_basis = freshness_stamp(stamp, source_value) if stamp is not None else None
        self.sources[name] = {
            "status": status, "as_of": stamp.isoformat() if stamp else None,
            "age_hours": round((self.now - age_basis).total_seconds() / 3600, 1) if age_basis else None,
            "precision": "date" if isinstance(source_value, str) and len(source_value) == 10 else "datetime",
        }
        if name == "nng8":
            self.sources[name]["receipt_status"] = "verified" if research_record else "missing"
            if research_record:
                self.sources[name].update(received_at=research_record["stored_at"],
                    content_sha256=research_record["content_sha256"],
                    receiver_record_id=research_record["record_id"])
        if name == "jd":
            self.sources[name].update(self.news_receipt(raw))
            received = parse_date(self.sources[name].get("received_at"))
            if status == "future" or received and received > self.now:
                self.issue(name, "future")
                return None, ""
        return data, md or ""

    def item_time(self, name: str, item: dict, provenance: dict) -> tuple[str, bool]:
        # Event windows and receipt clocks are not publication timestamps.
        stamp, value, field = None, None, ""
        invalid = False
        candidates = []
        for mapping in (item, provenance):
            publication = mapping.get("published_at")
            if publication is None or isinstance(publication, str) and not publication.strip():
                continue
            candidates.append((mapping, "published_at"))
        if not candidates:
            for mapping in (item, provenance):
                key = next((key for key in ("timestamp", "as_of", "date") if key in mapping), None)
                if key is not None:
                    candidates.append((mapping, key))
        for mapping, key in candidates:
            value = mapping[key]
            stamp = parse_date(value)
            if name == "jd" and isinstance(value, str) and len(value) > 10:
                try:
                    stamp = news_history.precise_time(value).astimezone(TZ)
                except news_history.HistoryError:
                    stamp = None  # Do not guess a timezone for a publication claim.
            if stamp is not None:
                field = key
                break
            invalid = True
            self.issue(name, "invalid")
        eligible = not (name == "jd" and self.news_snapshot is not None and invalid)
        label = ""
        if name == "jd" and field != "published_at":
            label = "发布时间未知"
            info = self.sources[name]
            info["publication_unknown_count"] = info.get("publication_unknown_count", 0) + 1
        if stamp is not None:
            only_date = isinstance(value, str) and len(value) == 10
            shown = stamp.strftime("%Y-%m-%d") + "（仅日期）" if only_date else stamp.strftime("%Y-%m-%d %H:%M:%S")
            if name == "jd":
                label += ("发布 " if field == "published_at" else "；条目数据时间 ") + shown
            else:
                label = shown
            if stamp > self.now:
                self.issue(name, "future")
                if name == "jd":
                    eligible = False
            elif self.now - freshness_stamp(stamp, value) > timedelta(hours=72):
                self.issue(name, "stale")
        if not eligible:
            info = self.sources[name]
            info["excluded_time_count"] = info.get("excluded_time_count", 0) + 1
        return (f"（{label}）" if label else ""), eligible

    def highlights(self, name: str) -> list[str]:
        if name == "nng8" and self.require_sealed_research and self.now.weekday() >= 5:
            self.sources[name] = {"status": "not_scheduled", "as_of": None, "age_hours": None}
            return ["研究：周末没有计划生成新研究，本栏不沿用旧稿。"]
        data, md = self.source(name)
        if name == "nng8" and self.require_sealed_research:
            return self.sealed_research(data)
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
            seen_news: set[str] = set()
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
                provenance = item.get("provenance", {})
                if not isinstance(provenance, dict):
                    self.issue(name, "schema_error")
                    provenance = {}
                event_key = provenance.get("event_id")
                if is_noise(provenance) or (isinstance(event_key, str) and is_noise({"idempotency_key": event_key})):
                    filtered += 1
                    continue
                title = first_text(item, ("title", "note", "reason", "summary")) or first_text(provenance, ("title", "note", "reason", "summary"))
                symbol = clean(item.get("symbol") or item.get("code"), 24)
                if not symbol and not title:
                    self.issue(name, "schema_error")
                    invalid_items += 1
                    continue
                kind = instrument_kind(symbol)
                index_recap = bool(title and "上证指数" in title and "深证成指" in title and "收报" in title)
                if index_recap or (kind == "index" and not title):
                    continue
                shown = display_instrument(
                    symbol, first_text(item, ("name", "short_name", "sec_name"), 12),
                ) if symbol else ""
                if kind == "index":
                    shown = INDEX_NAMES[symbol]
                line = "｜".join(part for part in (shown, side(item.get("side")) if symbol and kind != "index" else "", title) if part)
                time_text, eligible = self.item_time(name, item, provenance)
                if not eligible:
                    continue
                line += time_text
                publisher = first_text(item, ("source",), 60) or first_text(provenance, ("source",), 60)
                if publisher:
                    line += f"｜来源：{publisher}"
                link = next((safe_url(mapping.get(key)) for mapping in (item, provenance)
                             for key in ("url", "link", "source_url") if safe_url(mapping.get(key))), "")
                if link:
                    line += f" {link}"
                # Validate every item's date/provenance before deduplication.
                # Rendered text is lossy and cannot establish news identity.
                if name == "jd":
                    key = news_item_key(item, provenance)
                    if key in seen_news:
                        continue
                    seen_news.add(key)
                    kept.append(line)
                elif line not in kept:
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
        rendered = self.text_highlights(name, text)
        if name == "jd":
            rendered.insert(0, "发布时间未知（正文未提供结构化时间）。")
        return rendered

    def text_highlights(self, name: str, text: str) -> list[str]:
        # Build bounded semantic blocks: a watch-list label and its bullets are
        # one unit; market and turnover form one overview. Preserve input order
        # within each section. No model, importance score, or invented analysis.
        blocks: list[tuple[str, str]] = []
        notes: list[tuple[int, str]] = []
        shortened = False
        collecting = ""
        fenced = False
        for raw_line in text.splitlines()[:MAX_ROWS]:
            if raw_line.strip().startswith("```"):
                fenced = not fenced
                collecting = ""
                continue
            if fenced:
                continue
            full_value = clean(raw_line.strip().lstrip("#*- "), MAX_BODY)
            value = clean(full_value)
            if len(full_value) > MAX_TEXT:
                shortened = True
            if is_noise({"idempotency_key": value}) or re.search(r"(?i)(selftest|e2e-forward|nginx-probe|dedirock:probe|\bprobe[:=])", value):
                continue
            if not value or raw_line.strip().startswith("{") or re.match(r"(?i)^(as_of|date|日期|数据日期|来源日期|生成时间)\s*[:：=]", value):
                continue
            if re.fullmatch(r"(?:研究要点|研究摘要|研究日报|市场日报|A股联动日报)(?:\s*[（(].*[）)]|\s+\d{4}-\d{2}-\d{2})?", value):
                continue
            if name == "nng8" and re.match(r"^(?:数据|说明|数据说明|风险提示|风险|限制|注意事项|免责声明)[：:]|(?i:^research-data\s*快照\s*[：:])", value):
                collecting = ""
                rejected = bool(re.match(r"(?i)^research-data\s*快照\s*[：:]\s*(?:rejected|failed|invalid|未通过|拒绝)(?:\b|$)", value))
                limited = bool(re.match(r"^(?:风险提示|风险|限制|注意事项|免责声明)[：:]", value)
                               or re.search(r"部分可用|缺失|未验证|暂不采用|不完整|异常|失败", value))
                priority = 0 if rejected else 1 if limited else 2
                if not any(note == value for _, note in notes):
                    notes.append((priority, value))
                if rejected:
                    self.issue(name, "invalid")
                continue
            section = research_section(value) if name == "nng8" else ""
            bullet = bool(re.match(r"^\s*(?:[-*+]\s+|\d+[.)、]\s*)", raw_line))
            if collecting and bullet and not section and blocks:
                old_section, old = blocks[-1]
                combined = old.rstrip("：:") + "；" + value
                shortened |= len(combined) > MAX_TEXT
                blocks[-1] = old_section, clean(combined)
                continue
            collecting = section if value.endswith(("：", ":")) and section else ""
            if section == "market" and blocks and blocks[-1][0] == "market":
                combined = blocks[-1][1] + "；" + value
                shortened |= len(combined) > MAX_TEXT
                blocks[-1] = "market", clean(combined)
            elif (section, value) not in blocks:
                blocks.append((section, value))
        # Prefer one block from each labelled section over repeated market
        # metrics. Unlabelled free text retains its original first-three order.
        selected: list[int] = []
        seen_sections: set[str] = set()
        for index, (section, value) in enumerate(blocks):
            if section and section not in seen_sections:
                selected.append(index)
                seen_sections.add(section)
        selected = selected[:3]
        for index in range(len(blocks)):
            if len(selected) == 3:
                break
            if index not in selected:
                selected.append(index)
        lines = [f"- {blocks[index][1]}" for index in sorted(selected)]
        if not blocks:
            self.issue(name, "schema_error")
        # Rejection/limitations must survive a flood of ordinary source notes.
        note_indexes = sorted(range(len(notes)), key=lambda index: (notes[index][0], index))[:3]
        note_lines = [f"来源提示：{notes[index][1]}" for index in sorted(note_indexes)]
        if len(notes) > 3:
            self.issue(name, "truncated")
            note_lines.append(f"来源提示：另有 {len(notes) - 3} 条说明未展示，已优先保留拒绝及限制提示。")
        if shortened:
            self.issue(name, "truncated")
            note_lines.append("来源提示：部分段落或观察清单已截断，不能视为完整结论。")
        return ([self.highlight_heading(name)] + (lines or ["未知：未解析出有效要点。"])
                + note_lines)

    def sealed_research(self, data: dict | None) -> list[str]:
        """Require a completed current-day sealed-report relay, never old text.

        Hashes here are provenance identifiers, not independent verification of
        the remote files. The authenticated relay verifies the actual seal.
        """
        unavailable = ["研究：尚未收到当日通过校验的正式研究摘要；旧稿未纳入。"]
        contract = data.get("research_contract") if isinstance(data, dict) else None
        summary = data.get("research_summary") if isinstance(data, dict) else None
        if not isinstance(data, dict) or not isinstance(contract, dict) or not isinstance(summary, dict):
            self.issue("nng8", "missing")
            return unavailable
        try:
            if is_noise(data):
                raise ValueError
            day = self.now.date().isoformat()
            if (type(contract.get("version")) is not int or contract["version"] != 1
                    or contract.get("status") != "ready" or data.get("source") != "nng8"
                    or contract.get("report_date") != day or data.get("target_date") != day
                    or contract.get("publication_gate") != "passed"
                    or contract.get("evidence_review") != "passed"
                    or contract.get("data_status") not in ("live", "partial")
                    or contract.get("semantic_review") not in ("not_human_reviewed", "human_passed")):
                raise ValueError
            as_of = contract.get("as_of")
            if not isinstance(as_of, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of) is None:
                raise ValueError
            market = parse_date(as_of)
            if market is None or market.date() >= self.now.date() or data.get("as_of") != as_of:
                raise ValueError
            # Validity of the actual previous session was checked by the sealed
            # upstream report, not inferred using a weekday-only calendar here.
            for key in ("report_sha256", "last_run_sha256", "context_sha256"):
                value = contract.get(key)
                if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                    raise ValueError
            times = []
            for value in (contract.get("completed_at"), data.get("produced_at")):
                if not isinstance(value, str) or re.search(r"(?:Z|[+-]\d{2}:\d{2})$", value) is None:
                    raise ValueError
                stamp = parse_date(value)
                if stamp is None or stamp.date() != self.now.date() or stamp > self.now:
                    raise ValueError
                times.append(stamp)
            if times[0] > times[1]:
                raise ValueError
            def text_value(value):
                if not isinstance(value, str) or not value.strip() or len(value) > 1200:
                    raise ValueError
                result = clean(value, 1200)
                if not result:
                    raise ValueError
                return result
            conclusion = text_value(summary.get("conclusion"))
            if re.match(r"^(?:-\s*)?\[(?:事实|推断)\]\s*\S", conclusion) is None:
                raise ValueError
            parts = {}
            for field in ("evidence", "counter_evidence", "risks", "sources", "limitations"):
                values = summary.get(field)
                if not isinstance(values, list) or len(values) > 24:
                    raise ValueError
                parts[field] = [text_value(value) for value in values]
            if len(conclusion) + sum(len(v) for values in parts.values() for v in values) > 20000:
                raise ValueError
            if not parts["risks"] or not parts["limitations"] or not parts["sources"]:
                raise ValueError
            source_ids = set()
            for entry in parts["sources"]:
                match = re.fullmatch(r"(?:\[来源\s*([1-9][0-9]*)\]|([1-9][0-9]*)[.、])\s*(\S.*)", entry)
                if match is None:
                    raise ValueError
                identifier = match[1] or match[2]
                if identifier in source_ids:
                    raise ValueError
                source_ids.add(identifier)
            referenced = research_references("\n".join(
                [conclusion] + parts["evidence"] + parts["counter_evidence"]
                + parts["risks"] + parts["limitations"]))
            if not referenced or not referenced.issubset(source_ids):
                raise ValueError
            claim_references = research_references("\n".join([conclusion] + parts["evidence"]))
            if not claim_references:
                raise ValueError
            counter_missing = False
            if "[推断]" in conclusion:
                if not parts["evidence"] or not parts["counter_evidence"]:
                    raise ValueError
                if any(not research_references(value) for value in parts["evidence"]):
                    raise ValueError
                for value in parts["counter_evidence"]:
                    if research_references(value):
                        continue
                    if (re.match(r"^(?:-\s*)?\[推断\]\s*风险[：:]", conclusion) is None
                            or re.fullmatch(r"(?:反证[：:]\s*)?未提供结构化反证来源，不升级为事实结论。", value) is None
                            or len(parts["counter_evidence"]) != 1):
                        raise ValueError
                    counter_missing = True
        except (TypeError, ValueError, KeyError, OverflowError):
            self.issue("nng8", "invalid")
            return unavailable + ["来源提示：研究日期、封存凭证或摘要结构未通过校验。"]
        lines = [f"研究（报告日 {day}；行情截至 {as_of}；上游完成 {times[0].strftime('%H:%M')}）：", conclusion]
        for key, label in (("evidence", "支持证据"), ("counter_evidence", "反证"),
                           ("risks", "风险与数据缺口"), ("sources", "引用来源与时间"),
                           ("limitations", "来源限制")):
            if parts[key]:
                lines.append(label + "：")
                lines.extend("- " + value for value in parts[key])
        lines.append("数据状态：" + ("部分可用" if contract["data_status"] == "partial" else "上游标记可用"))
        lines.append("研究语义：" + ("未经人工审核" if contract["semantic_review"] == "not_human_reviewed" else "上游标记人工审核通过"))
        if counter_missing:
            lines.append("来源限制：该风险推断未提供可引用反证，固定缺失提示不代表反证已核验。")
        lines.append("仅供研究核对，不构成审批、交易指令或已核验执行结果。")
        return lines

    def highlight_heading(self, name: str) -> str:
        if f"{name}:stale" in self.issues:
            scope = "含过期历史内容，不作新增"
        elif any(f"{name}:{code}" in self.issues for code in ("future", "unknown_date", "invalid")):
            scope = "内容或日期异常，不作新增"
        else:
            scope = "本次上报，非今日新增计数"
        return f"{LABELS[name]}（{scope}）："

    def reviews(self) -> list[str]:
        lines = ["【人审（今日）】"]
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
            symbol = clean(merged.get("symbol"), 24)
            if instrument_kind(symbol) == "index":
                continue
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
            reviewed_today = bool(review_time and review_time.date() == self.now.date())
            if decision in ("approve", "reject") and reviewed_today and len(review_results) < 3:
                state = "通过" if decision == "approve" else "拒绝"
                quant = merged.get("quant_status")
                delivered = "已提交/受理，非成交" if quant in ("ok", "accepted", "submitted") else "投递失败" if quant in ("failed", "error") else "未提交（已过期）" if quant == "skipped_expired" else "执行结果未知"
                shown = display_instrument(symbol, first_text(merged, ("name", "short_name"), 12))
                review_results.append(f"- {shown}｜{state}｜{delivered}")
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
            shown = display_instrument(
                clean(item.get("symbol"), 24), first_text(item, ("name", "short_name"), 12),
            )
            lines.append(f"- {shown}｜{side(item.get('side'))}｜有效至 {expiry.strftime('%Y-%m-%d %H:%M')}")
        if review_results:
            lines += ["今日审核结果："] + review_results
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
                symbol = clean(item.get("symbol"), 24)
                if instrument_kind(symbol) == "index":
                    continue
                status = item.get("status")
                key = status if status in ("accepted", "submitted", "rejected", "filled") else "unknown"
                if key == "unknown" and not isinstance(status, (str, type(None))):
                    self.issue("quant", "schema_error")
                counts[key] += 1
                if len(details) < 3:
                    label = {"accepted": "受理（非成交）", "submitted": "提交（非成交）", "rejected": "拒绝", "filled": "上报成交（未核验）", "unknown": "状态未知"}[key]
                    details.append(f"- {display_instrument(symbol) or '标的未知'}｜{label}")
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
        names = harvest_names(*research, *news, *review)
        research = [decorate_symbols(line, names) for line in research]
        news = [decorate_symbols(line, names) for line in news]
        review = [decorate_symbols(line, names) for line in review]
        execution = [decorate_symbols(line, names) for line in execution]
        joined_execution = "\n".join(execution)
        if ("所报执行为模拟/探针" in joined_execution
                or "受理 0 / 提交 0 / 拒绝 0 / 上报成交 0 / 状态未知 0" in joined_execution):
            execution = []

        nng8 = self.sources.get("nng8") or {}
        research_stamp = parse_date(nng8.get("as_of"))
        if nng8.get("status") == "not_scheduled":
            research_day = "周末无新研究"
        elif "nng8:missing" in self.issues:
            research_day = "缺报"
        elif nng8.get("status") == "ok" and research_stamp:
            research_day = research_stamp.strftime("%Y-%m-%d")
        else:
            research_day = "未通过校验"

        if self.pending_count:
            today = f"今日：待审 {self.pending_count} · 研报日 {research_day}"
        elif self.pending_count == 0:
            today = f"今日：待审 0 · 研报日 {research_day}"
        else:
            today = f"今日：有效待审总量未知，请先核对数据。研报日 {research_day}"
        if "jd:missing" in self.issues:
            today += " · 资讯缺报"
        lines = [
            f"A股联动日报 {self.now.date().isoformat()}",
            f"生成 {self.now.strftime('%H:%M')}（北京时间）｜研报日 {research_day}",
            "", "【今日关注】", today, "",
            "【研究】", *research, "", "【资讯】", *news, "", *review,
        ]
        if execution:
            lines += ["", *execution]
        lines += ["", "【来源日期与警示】"]
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
                if name == "nng8" and info["status"] == "not_scheduled":
                    lines.append("研究：周末未计划生成新研究。")
                elif name == "nng8" and "nng8:missing" in self.issues:
                    lines.append("研究：当日密封摘要缺报，旧稿未纳入。")
                else:
                    detail = f"来源时间 {label}（北京时间）"
                    if stamp and info.get("precision") == "date":
                        detail = f"数据日期 {stamp.strftime('%Y-%m-%d')}（未提供时分）"
                    if name == "jd":
                        received = parse_date(info.get("received_at"))
                        reception = received.strftime('%Y-%m-%d %H:%M:%S') + "（北京时间）" if received else "未知（无匹配收件记录）"
                        detail += f"；本机接收 {reception}"
                    lines.append(f"{LABELS[name]}：{detail}。")
        if self.issues:
            lines.append("警示：" + "；".join(f"{LABELS[source]}{WARNINGS[code]}" for source, code in (item.split(":") for item in self.issues)) + "。")
        else:
            lines.append("研究按当日报告校验，其余来源按72小时阈值检查。" if self.require_sealed_research
                         else "来源不超过72小时；前一自然日和周末日期按此阈值检查。")
        lines.append("说明：缺报不沿用旧稿；指数不作为个股；过滤模拟/探针。不构成交易指令。")
        return {"report_date": self.now.date().isoformat(), "text": "\n".join(lines) + "\n", "issues": self.issues, "degraded": bool(self.issues), "sources": self.sources}


def build_report(base: Path, db_path: Path, now: datetime | None = None, *, require_sealed_research: bool = True) -> dict:
    now = datetime.now(TZ) if now is None else now
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    return Renderer(Path(base), Path(db_path), now, require_sealed_research=require_sealed_research).report()
