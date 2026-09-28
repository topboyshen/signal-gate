"""Pure nng8 contract validation shared by intake and presentation.

HMAC is checked by the HTTP boundary. Hash identifiers attest the signed
upstream evidence; this receiver does not pretend to independently fetch it.
"""
from datetime import date, datetime
import hashlib
import json
import re
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
HEX = re.compile(r"[0-9a-f]{64}\Z")
PARTS = ("evidence", "counter_evidence", "risks", "sources", "limitations")
ROLES = {"上涨占优", "下跌占优", "成交额", "单日行情", "报告期利润同比"}
MISSING_LIMITS = {"未提供结构化反证来源；此限制只约束本条推断，不否定已核验事实。",
                  "未提供结构化反证来源，不升级为事实结论。"}


def require(condition, reason="invalid_research_contract"):
    if not condition:
        raise ValueError(reason)


def day(value):
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None)
    return date.fromisoformat(value)


def stamp(value):
    require(isinstance(value, str))
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None and result.utcoffset() is not None)
    return result.astimezone(TZ)


def text(value):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= 1200)
    return value.strip()


def references(value):
    pattern = re.compile(r"\[来源\s*([1-9][0-9]*(?:(?:\s*[、,，]\s*|\s+)[1-9][0-9]*)*)\s*\]")
    result = set()
    for match in pattern.finditer(value):
        result.update(re.findall(r"[1-9][0-9]*", match[1]))
    require("[来源" not in pattern.sub("", value), "invalid_source_reference")
    return result


def _not_probe(data):
    key = str(data.get("idempotency_key") or "").lower()
    require(not key.startswith(("selftest", "review-selftest", "e2e-forward", "nginx-probe", "curl-valid",
            "dedirock:probe", "test:", "research_eod:nng8:probe", "probe")) and ":probe:" not in key)
    for name in ("simulation", "is_simulation", "simulated", "is_test", "test", "dry_run"):
        value = data.get(name)
        require(not (value is True or value == 1 or isinstance(value, str) and value.lower() in
                     ("true", "yes", "1", "simulation", "simulated")))
    require(not any(str(data.get(name) or "").lower() in
                    ("test", "simulation", "simulated", "paper", "sandbox", "demo", "dry_run")
                    for name in ("mode", "environment", "origin")))


def validate(data, now):
    """Return validated rendering fields, or raise ValueError; no IO or mutation."""
    require(isinstance(data, dict) and data.get("source") == "nng8")
    require(now.tzinfo is not None and now.utcoffset() is not None)
    now = now.astimezone(TZ)
    target = day(data.get("target_date"))
    require(target == now.date(), "target_date_not_current")
    _not_probe(data)
    if data.get("signal_type") == "research_skip" or "research_skip" in data:
        return validate_skip(data, now)
    contract, summary = data.get("research_contract"), data.get("research_summary")
    require(isinstance(contract, dict) and isinstance(summary, dict))
    require(type(contract.get("version")) is int and contract["version"] == 1
            and contract.get("status") == "ready" and contract.get("report_date") == target.isoformat()
            and contract.get("publication_gate") == "passed" and contract.get("evidence_review") == "passed"
            and contract.get("data_status") in ("live", "partial")
            and contract.get("semantic_review") in ("not_human_reviewed", "human_passed"))
    require(contract.get("target_date", target.isoformat()) == target.isoformat()
            and contract.get("source", "nng8") == "nng8")
    as_of = day(contract.get("as_of"))
    require(as_of < target and data.get("as_of") == as_of.isoformat())
    for key in ("report_sha256", "last_run_sha256", "context_sha256"):
        require(isinstance(contract.get(key), str) and HEX.fullmatch(contract[key]) is not None)
    require(data.get("report_sha256", contract["report_sha256"]) == contract["report_sha256"])
    completed, produced = stamp(contract.get("completed_at")), stamp(data.get("produced_at"))
    require(completed.date() == target and produced.date() == target and completed <= produced <= now)
    conclusion = text(summary.get("conclusion"))
    require(re.match(r"^(?:-\s*)?\[(?:事实|推断)\]\s*\S", conclusion) is not None)
    parts = {}
    for key in PARTS:
        values = summary.get(key)
        require(isinstance(values, list) and len(values) <= 24)
        parts[key] = [text(v) for v in values]
    require(len(conclusion) + sum(len(v) for items in parts.values() for v in items) <= 20000)
    require(bool(parts["risks"] and parts["limitations"] and parts["sources"]))
    source_ids = set()
    for entry in parts["sources"]:
        match = re.fullmatch(r"(?:\[来源\s*([1-9][0-9]*)\]|([1-9][0-9]*)[.、])\s*(\S.*)", entry)
        require(match is not None)
        identifier = match[1] or match[2]
        require(identifier not in source_ids)
        source_ids.add(identifier)
    refs = references("\n".join([conclusion] + sum((parts[k] for k in PARTS if k != "sources"), [])))
    require(bool(refs) and refs.issubset(source_ids))
    require(bool(references("\n".join([conclusion] + parts["evidence"]))))
    inference = "[推断]" in conclusion
    counter_missing = False
    analysis = summary.get("analysis_contract")
    if analysis is not None:
        require(isinstance(analysis, dict) and type(analysis.get("version")) is int and analysis["version"] == 2)
        require({"claim_type", "inference_status", "counter_evidence_status", "evidence_roles",
                 "period_note", "inference_limit"}.issubset(analysis))
        require(analysis.get("claim_type") == ("inference" if inference else "fact"))
        require(analysis.get("inference_status") == ("unverified" if inference else "not_applicable"))
        roles = analysis.get("evidence_roles")
        require(isinstance(roles, list) and len(roles) <= 24)
        evidence_refs = references("\n".join(parts["evidence"]))
        for role in roles:
            require(isinstance(role, dict) and isinstance(role.get("source_id"), str)
                    and isinstance(role.get("role"), str) and role["source_id"] in source_ids
                    and role["source_id"] in evidence_refs and role["role"] in ROLES)
        period = analysis.get("period_note")
        if period is not None:
            text(period)
            match = re.fullmatch(r"行情 (\d{4}-\d{2}-\d{2})；利润同比报告期 (\d{4}-\d{2}-\d{2})", period)
            require(match is not None and day(match[1]) < target and day(match[2]) < target)
            require("期间：" + period + "。" in parts["limitations"])
        state, limitation = analysis.get("counter_evidence_status"), analysis.get("inference_limit")
        if inference:
            require(bool(parts["evidence"]) and all(references(v) for v in parts["evidence"]))
            if state == "explicitly_missing":
                require(not parts["counter_evidence"] and limitation in MISSING_LIMITS
                        and any(v in (limitation, "推断限制：" + limitation) for v in parts["limitations"]))
                counter_missing = True
            else:
                require(state == "provided" and bool(parts["counter_evidence"])
                        and all(references(v) for v in parts["counter_evidence"]) and limitation is None)
        else:
            require(state == "not_applicable" and limitation is None)
    elif inference:
        require(bool(parts["evidence"] and parts["counter_evidence"]))
        require(all(references(v) for v in parts["evidence"]))
        for value in parts["counter_evidence"]:
            if references(value):
                continue
            require(re.match(r"^(?:-\s*)?\[推断\]\s*风险[：:]", conclusion) is not None
                    and re.fullmatch(r"(?:反证[：:]\s*)?未提供结构化反证来源，不升级为事实结论。", value) is not None
                    and len(parts["counter_evidence"]) == 1)
            counter_missing = True
        # Backward-compatible input, honest output: a limitation isn't evidence.
        if counter_missing:
            parts["limitations"] += parts["counter_evidence"]
            parts["counter_evidence"] = []
    return {"kind": "research", "day": target.isoformat(), "as_of": as_of.isoformat(),
            "completed": completed, "conclusion": conclusion, "parts": parts,
            "contract": contract, "analysis": analysis, "counter_missing": counter_missing}


def validate_skip(data, now):
    skip = data.get("research_skip")
    target = now.date()
    require(data.get("signal_type") == "research_skip" and "research_contract" not in data
            and "research_summary" not in data and data.get("as_of") == target.isoformat())
    require(isinstance(skip, dict) and type(skip.get("version")) is int and skip["version"] == 1
            and skip.get("status") == "not_scheduled" and skip.get("reason") == "non_trading_day"
            and skip.get("market") == "XSHG" and skip.get("target_date") == target.isoformat())
    produced, validated = stamp(data.get("produced_at")), stamp(skip.get("validated_at"))
    require(validated.date() == target and produced.date() == target and validated <= produced <= now)
    calendar, proof = skip.get("calendar"), skip.get("proof")
    require(isinstance(calendar, dict) and isinstance(proof, dict))
    require(set(calendar) == {"source_id", "target_date", "status", "previous_trading_date", "next_trading_date", "data_as_of"})
    require(calendar.get("source_id") == "akshare:sina:trade-calendar"
            and calendar.get("target_date") == target.isoformat() and calendar.get("status") == "non_trading_day"
            and day(calendar.get("previous_trading_date")) < target < day(calendar.get("next_trading_date")))
    collected = stamp(calendar.get("data_as_of"))
    require(collected.date() == target and collected <= validated)
    for key in ("occurrence_sha256", "last_run_sha256", "context_sha256", "calendar_sha256"):
        require(isinstance(proof.get(key), str) and HEX.fullmatch(proof[key]) is not None)
    canonical = json.dumps(calendar, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    require(hashlib.sha256(canonical).hexdigest() == proof["calendar_sha256"])
    return {"kind": "skip", "day": target.isoformat(), "as_of": target.isoformat(), "skip": skip}
