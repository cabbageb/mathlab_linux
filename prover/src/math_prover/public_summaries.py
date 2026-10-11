from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import re

from .vnext_contracts import ValidationResult


SUMMARY_MODES = {"event", "verified"}
FORBIDDEN = re.compile(
    r"\b(?:agent|thread(?:_id)?|token|runtimeevent|architecture_review|supervisor_review|node_[0-9a-f]+)\b|(?:^|[/\\])(?:input|output|runtime|turns)(?:[/\\]|$)",
    flags=re.IGNORECASE,
)
MATH_DELIMITERS = re.compile(r"\\\(.*?\\\)|\\\[.*?\\\]", flags=re.DOTALL)
BARE_TEX = re.compile(
    r"\$|\\[A-Za-z]+|(?<![A-Za-z0-9])(?:[A-Za-z])(?:[_^](?:\{[^{}\n]+\}|[-+A-Za-z0-9]+))+"
)
CHINESE_CHARACTERS = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


@dataclass(frozen=True)
class PublicSummarySource:
    source_key: str
    mode: str
    title: str
    content: str
    node_id: str | None = None
    source_role: str | None = None
    source_created_at: str | None = None


def summary_model_for_policy(execution_policy: dict[str, Any]) -> dict[str, str]:
    """从冻结 Run 档位选择摘要模型；旧 Run 缺角色快照时按档位确定性恢复。"""
    resolved = dict(execution_policy.get("resolved_role_models") or {})
    configured = resolved.get("public_summarizer")
    if isinstance(configured, dict) and configured.get("model") and configured.get("reasoning_effort"):
        return {
            "model": str(configured["model"]),
            "reasoning_effort": str(configured["reasoning_effort"]),
        }
    tier = str(execution_policy.get("model_tier") or execution_policy.get("live_mapping") or "")
    if tier == "deepseek_only":
        return {"model": "deepseek-v4-pro", "reasoning_effort": "low"}
    if tier == "qwen_only":
        return {"model": "qwen3.8-max", "reasoning_effort": "low"}
    return {"model": "gpt-5.6-luna", "reasoning_effort": "medium"}


def parse_public_summary(
    text: str,
    mode: str,
    *,
    allow_verified_overflow: bool = False,
    expected_language: str | None = None,
    allow_language_mismatch: bool = False,
) -> tuple[str | None, ValidationResult]:
    """校验公开摘要；Verified 的最终修复轮可只放宽展示长度。"""
    if mode not in SUMMARY_MODES:
        return None, ValidationResult([f"未知摘要模式：{mode}"], [])
    summary = " ".join(str(text or "").strip().strip("`\"'").split())
    errors: list[str] = []
    if not summary:
        errors.append("摘要不能为空")
    if re.search(r"^(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|\{)", summary):
        errors.append("单条摘要只能输出自然语言正文")
    maximum = 60 if mode == "event" else 120
    if len(summary) > maximum and not (mode == "verified" and allow_verified_overflow):
        errors.append(f"{mode} 摘要不得超过 {maximum} 个字符")
    if FORBIDDEN.search(summary):
        errors.append("摘要不得包含内部角色、事件名、thread、token、ID 或路径")
    outside_math = MATH_DELIMITERS.sub("", summary)
    if BARE_TEX.search(outside_math):
        errors.append("数学符号必须使用 Markdown 行内数学定界符 \\( ... \\)，不得裸写 TeX 或使用美元定界符")
    if mode == "event" and len(re.findall(r"[。！？!?]", summary)) > 1:
        errors.append("Event 摘要必须只有一句话")
    if mode == "verified" and len(re.findall(r"[。！？!?]", summary)) > 2:
        errors.append("Verified 摘要最多两句话")
    if (
        str(expected_language or "").strip().lower() == "en"
        and not allow_language_mismatch
        and CHINESE_CHARACTERS.search(summary)
    ):
        errors.append(
            "The current user requires English output. "
            "The summary must not contain Chinese characters."
        )
    validation = ValidationResult(errors, [])
    return (summary if validation.ok else None), validation


def public_summary_task(source: PublicSummarySource) -> str:
    """生成单条摘要任务，只暴露 Controller 允许公开的结构化数学材料。"""
    mode = "Event" if source.mode == "event" else "Verified"
    return f"""# {mode} summary task

## Public title

{source.title}

## Mathematical source

{source.content}

只输出符合 {mode} 模式要求的摘要正文。
"""


def public_summary_batch_task(sources: list[PublicSummarySource]) -> str:
    """把同一 Run 的历史公开材料组织成一次批量摘要调用。"""
    payload = [
        {
            "key": item.source_key,
            "mode": item.mode,
            "title": item.title,
            "content": item.content,
        }
        for item in sources
    ]
    return (
        "# Batch public summary task\n\n"
        "为下面每个条目生成相应 Event 或 Verified 摘要。只输出 key 完全一致的 JSON object。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def parse_public_summary_batch(
    text: str,
    sources: list[PublicSummarySource],
) -> tuple[dict[str, str] | None, ValidationResult]:
    """校验批量 JSON 的 key 完备性，并逐条复用单摘要契约。"""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    errors: list[str] = []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, ValidationResult([f"批量摘要不是合法 JSON：{exc}"], [])
    if not isinstance(payload, dict):
        return None, ValidationResult(["批量摘要必须是 JSON object"], [])
    expected = {item.source_key: item for item in sources}
    if set(payload) != set(expected):
        errors.append(
            "批量摘要 key 不匹配："
            f"missing={sorted(set(expected) - set(payload))}, extra={sorted(set(payload) - set(expected))}"
        )
    parsed: dict[str, str] = {}
    for key, source in expected.items():
        value, validation = parse_public_summary(str(payload.get(key) or ""), source.mode)
        errors.extend(f"{key}: {item}" for item in validation.errors)
        if value is not None:
            parsed[key] = value
    validation = ValidationResult(errors, [])
    return (parsed if validation.ok else None), validation


def fallback_public_summary(
    source: PublicSummarySource, *, language: str = "zh"
) -> str:
    """模型摘要最终失败时提供稳定短句，绝不截取并展示原始长报告。"""
    if language == "en":
        if source.mode == "verified":
            return "This result passed independent verification; open it to inspect the full statement and scope."
        if source.source_role == "architecture_reviewer":
            return "The global research route was reviewed again; a detailed summary is not yet available."
        return "The local research group reached a new stage judgment; a detailed summary is not yet available."
    if source.mode == "verified":
        return "该结论已经通过独立核验，完整命题与适用边界可点击查看。"
    if source.source_role == "architecture_reviewer":
        return "全局研究路线已完成新一轮复盘，详细判断暂未生成摘要。"
    return "局部研究组已形成新的阶段判断，详细结论暂未生成摘要。"
