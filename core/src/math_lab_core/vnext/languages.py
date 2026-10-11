from __future__ import annotations


LANGUAGE_CHINESE = "zh"
LANGUAGE_ENGLISH = "en"
LANGUAGES = {LANGUAGE_CHINESE, LANGUAGE_ENGLISH}


def normalize_language(value: str | None, *, default: str = LANGUAGE_CHINESE) -> str:
    """规范化中英文语言选项；只接受产品公开的 zh/en 及其完整名称别名。"""
    normalized = str(value or default).strip().lower()
    aliases = {
        "zh": LANGUAGE_CHINESE,
        "zh-cn": LANGUAGE_CHINESE,
        "chinese": LANGUAGE_CHINESE,
        "中文": LANGUAGE_CHINESE,
        "en": LANGUAGE_ENGLISH,
        "en-us": LANGUAGE_ENGLISH,
        "english": LANGUAGE_ENGLISH,
        "英文": LANGUAGE_ENGLISH,
    }
    if normalized not in aliases:
        raise ValueError(f"不支持的语言选项：{value}")
    return aliases[normalized]


def output_language_instruction(language: str | None, *, domain: str) -> str:
    """仅在 English 模式追加输出约束；中文模式完全保持既有 Prompt 行为。"""
    if normalize_language(language) != LANGUAGE_ENGLISH:
        return ""
    if domain == "contract":
        return """# Contract output language

This Contract Conversation is frozen to English. Write every textual output in English only, including internal deliverables returned to the Controller, auditor reports, Contract Chat replies, Draft Patches, and all prose inside the Research Contract. Preserve required schema headings, identifiers, formulas, code, bibliographic titles, and verbatim user/source quotations when exact preservation is mathematically necessary. Do not switch to Chinese merely because the user request or an earlier message is Chinese.
"""
    if domain == "research":
        return """# Research output language

This Research Run is frozen to English. Write every textual output in English only, including Worker reports, Supervisor reviews, Findings, candidate and verified claim statements, proofs, Reviewer decisions, public summaries, Node cards, and all other user-visible or persisted research prose. Preserve formulas, identifiers, code, bibliographic titles, and verbatim source quotations when exact preservation is mathematically necessary. Internal hidden reasoning language is not constrained. The final mathematical paper remains English as required by the publication contract.
"""
    raise ValueError(f"未知语言约束域：{domain}")
