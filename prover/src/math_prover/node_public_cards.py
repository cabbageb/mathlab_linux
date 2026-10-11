from __future__ import annotations

from dataclasses import dataclass
import re

from .vnext_contracts import ValidationResult


_FORBIDDEN = re.compile(
    r"\b(?:agent|thread(?:_id)?|token|runtime|worker|supervisor|reviewer|node_[0-9a-f]+|claim_[0-9a-f]+)\b",
    flags=re.IGNORECASE,
)
_MATH_DELIMITERS = re.compile(r"\\\(.*?\\\)|\\\[.*?\\\]", flags=re.DOTALL)
_BARE_TEX = re.compile(r"\$|\\[A-Za-z]+|(?<![A-Za-z0-9])(?:[A-Za-z])(?:[_^](?:\{[^{}\n]+\}|[-+A-Za-z0-9]+))+")
_CHINESE_CHARACTERS = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


@dataclass(frozen=True)
class NodePublicCard:
    """保存面向数学用户的短标题与研究问题说明。"""

    title: str
    summary: str
    raw_markdown: str


def _section(markdown: str, heading: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        markdown,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def parse_node_public_card(
    markdown: str,
    *,
    expected_language: str | None = None,
    allow_language_mismatch: bool = False,
) -> tuple[NodePublicCard | None, ValidationResult]:
    """校验 Node 公开卡片，保留数学公式并排除内部运行语言。"""
    text = str(markdown or "").strip()
    errors: list[str] = []
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first != "# Research Node 展示":
        errors.append("首个非空行必须是 `# Research Node 展示`")
    title = " ".join(_section(text, "标题").split())
    summary = _section(text, "当前研究问题")
    if not title:
        errors.append("缺少 `## 标题`")
    if not summary:
        errors.append("缺少 `## 当前研究问题`")
    visible = f"{title}\n{summary}"
    if _FORBIDDEN.search(visible):
        errors.append("公开卡片不得包含角色、thread、token、Runtime 或内部 ID")
    outside_math = _MATH_DELIMITERS.sub("", visible)
    if _BARE_TEX.search(outside_math):
        errors.append("数学符号必须使用 `\\( ... \\)` 或 `\\[ ... \\]`")
    if (
        str(expected_language or "").strip().lower() == "en"
        and not allow_language_mismatch
        and _CHINESE_CHARACTERS.search(visible)
    ):
        errors.append(
            "The current user requires English output. "
            "The Node card title and body must not contain Chinese characters."
        )
    validation = ValidationResult(errors, [])
    return (
        NodePublicCard(title=title, summary=summary, raw_markdown=text + "\n")
        if validation.ok
        else None,
        validation,
    )


def node_public_card_task(*, kind: str, title: str, objective: str) -> str:
    """把内部 Node Card 裁成摘要模型所需的最小数学输入。"""
    return f"""# Research Node public-card task

## Node kind

{kind}

## Original title

{title}

## Exact mathematical objective

{objective}

只输出 Prompt 规定的 `# Research Node 展示` Markdown。
"""
