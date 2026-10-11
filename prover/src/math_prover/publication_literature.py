from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re

from .vnext_contracts import ValidationResult


SCOUT_TITLES = {
    "adversarial_prior_art_scout": "# Prior-Art Assessment",
    "related_work_context_scout": "# Related-Work and Mathematical Context Report",
}
EVIDENCE_LEVELS = {"FULL_TEXT_VERIFIED", "ABSTRACT_VERIFIED"}
_ARXIV_ID = re.compile(
    r"(?i)(?:arXiv\s*:\s*)?"
    r"((?:[0-9]{4}\.[0-9]{4,5}|[a-z-]+(?:\.[a-z]{2})?/[0-9]{7})(?:v[0-9]+)?)"
)
_SOURCE_BLOCK = re.compile(r"(?ms)^###\s+.+?(?=^###\s+|^##\s+|\Z)")
_EVIDENCE_LINE = re.compile(
    r"(?im)^\s*(?:[-*]\s+)?(?:\*\*)?Evidence level"
    r"(?:\*\*)?\s*[:.]\s*(.+?)\s*$"
)


def _evidence_level(text: str) -> str | None:
    match = _EVIDENCE_LINE.search(text)
    if match is None:
        return None
    return _normalize_evidence_value(match.group(1))


def _normalize_evidence_value(value: str) -> str:
    """识别规范标签及其同行括号说明，但不把第三种标签误归一化。"""
    normalized = value.strip().strip("`*_ .")
    for level in EVIDENCE_LEVELS:
        if normalized == level or normalized.startswith(level + " ") or normalized.startswith(level + "("):
            return level
    return normalized


def validate_scout_markdown(
    role: str,
    text: str,
) -> tuple[str | None, ValidationResult]:
    """核验 Scout 的人类可读报告入口与唯一证据等级，不把报告降格成 JSON。"""
    normalized = text.strip() + "\n"
    errors: list[str] = []
    expected = SCOUT_TITLES[role]
    first = next((line.strip() for line in normalized.splitlines() if line.strip()), "")
    if first != expected:
        errors.append(f"首个非空行必须逐字为 `{expected}`。")
    if len(normalized) < 900:
        errors.append("文献审读报告过短，尚未形成可供数学作者使用的实质比较。")
    for match in _EVIDENCE_LINE.finditer(normalized):
        level = _normalize_evidence_value(match.group(1))
        if level not in EVIDENCE_LEVELS:
            errors.append(
                "来源证据等级只能是 `FULL_TEXT_VERIFIED` 或 `ABSTRACT_VERIFIED`。"
            )
    return (
        normalized if not errors else None,
        ValidationResult(errors, [], format_name="publication literature Markdown"),
    )


def validate_scout_evidence(
    report: str,
    evidence: dict[str, object] | None,
) -> ValidationResult:
    """把报告中的 arXiv 身份与真实搜索、全文下载和阅读证据逐项绑定。"""
    errors: list[str] = []
    payload = dict(evidence or {})
    if payload.get("kind") != "literature":
        return ValidationResult(["Scout 缺少 Controller 文献证据 manifest。"])
    if int(payload.get("theorem_searches") or 0) < 1:
        errors.append("Scout 未执行 Controller 托管的 theorem search。")
    if int(payload.get("arxiv_searches") or 0) < 1:
        errors.append("Scout 未执行 Controller 托管的 arXiv/abstract search。")

    operation_root = Path(str(payload.get("operation_root") or ""))
    searched: set[str] = set()
    search_root = operation_root / "literature" / "search"
    if search_root.is_dir():
        for path in sorted(search_root.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in list(data.get("results") or []):
                raw = str(dict(item).get("arxiv_id") or "")
                if raw:
                    searched.add(raw.rsplit("v", 1)[0])

    full = {
        str(dict(item).get("arxiv_id") or "")
        for item in list(payload.get("papers") or [])
        if dict(item).get("pdf_sha256") or dict(item).get("source_sha256")
    }
    read_count = int(payload.get("reading_evidence_count") or 0)
    if full and read_count < len(full):
        errors.append("已物化论文数量超过可追溯 reading evidence 数量。")

    for block in _SOURCE_BLOCK.findall(report):
        level = _evidence_level(block)
        if level is None:
            continue
        identifiers = _ARXIV_ID.findall(block)
        if not identifiers:
            errors.append("每个带证据等级的来源条目都必须给出真实 arXiv ID。")
            continue
        identifier = identifiers[0]
        base = identifier.rsplit("v", 1)[0]
        if level == "FULL_TEXT_VERIFIED":
            exact = next((item for item in identifiers if re.search(r"v[0-9]+$", item)), "")
            if not exact or exact not in full:
                errors.append(
                    f"来源 `{identifier}` 标为 FULL_TEXT_VERIFIED，但没有匹配的 exact-version 下载与阅读证据。"
                )
        elif level == "ABSTRACT_VERIFIED" and base not in searched:
            errors.append(
                f"来源 `{identifier}` 标为 ABSTRACT_VERIFIED，但不在本 Scout 的冻结搜索结果中。"
            )
    return ValidationResult(errors, [], format_name="publication literature evidence")


def scout_source_count(report: str) -> int:
    """统计报告中具有合法证据等级的唯一 arXiv 来源，供 LaTeX References 门禁使用。"""
    identifiers: set[str] = set()
    for block in _SOURCE_BLOCK.findall(report):
        if _evidence_level(block) not in EVIDENCE_LEVELS:
            continue
        matches = _ARXIV_ID.findall(block)
        if matches:
            identifiers.add(matches[0].rsplit("v", 1)[0])
    return len(identifiers)


def filter_scout_report_to_frozen_evidence(
    report: str,
    evidence: dict[str, object],
) -> tuple[str, int, list[str]]:
    """删除无法绑定冻结证据的来源块，并生成 Writer 可执行的唯一 citation 边界。"""
    operation_root = Path(str(evidence.get("operation_root") or ""))
    searched: set[str] = set()
    search_root = operation_root / "literature" / "search"
    if search_root.is_dir():
        for path in sorted(search_root.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in list(data.get("results") or []):
                raw = str(dict(item).get("arxiv_id") or "")
                if raw:
                    searched.add(raw.rsplit("v", 1)[0])
    full = {
        str(dict(item).get("arxiv_id") or "")
        for item in list(evidence.get("papers") or [])
        if dict(item).get("pdf_sha256") or dict(item).get("source_sha256")
    }

    accepted_ids: set[str] = set()
    removed: list[str] = []
    chunks: list[str] = []
    cursor = 0
    for match in _SOURCE_BLOCK.finditer(report):
        chunks.append(report[cursor:match.start()])
        block = match.group(0)
        level = _evidence_level(block)
        keep = True
        identifier = ""
        if level is not None:
            identifiers = _ARXIV_ID.findall(block)
            identifier = identifiers[0] if identifiers else ""
            base = identifier.rsplit("v", 1)[0] if identifier else ""
            if level not in EVIDENCE_LEVELS or not identifier:
                keep = False
            elif level == "FULL_TEXT_VERIFIED":
                exact = next((item for item in identifiers if re.search(r"v[0-9]+$", item)), "")
                keep = bool(exact and exact in full)
            else:
                keep = base in searched
            if keep:
                accepted_ids.add(base)
            else:
                removed.append(identifier or f"unsupported level {level}")
        if keep:
            chunks.append(block)
        cursor = match.end()
    chunks.append(report[cursor:])
    filtered = "".join(chunks).strip() + "\n"
    lines = filtered.splitlines()
    boundary = [
        "",
        "## Controller-validated citation boundary",
        "",
        "Only the following arXiv sources are eligible for Writer citation from this report:",
        "",
        *(f"- `{identifier}`" for identifier in sorted(accepted_ids)),
        "",
    ]
    if removed:
        boundary.extend(
            [
                "Source blocks without matching frozen search/full-text evidence were omitted",
                "from this Writer-facing copy. The original Scout report remains preserved.",
                "",
            ]
        )
    filtered = "\n".join([lines[0], *boundary, *lines[1:]]).rstrip() + "\n"
    return filtered, len(accepted_ids), removed
