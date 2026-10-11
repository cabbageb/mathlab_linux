from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import re


TITLE = "Research Contract"

SECTION_SPECS: tuple[tuple[str, int, str], ...] = (
    ("mathematical_contract", 2, "A. Mathematical Contract"),
    ("research_objective", 3, "Research Objective"),
    ("objects_assumptions", 3, "Mathematical Objects and Assumptions"),
    ("unknown_answer_slots", 3, "Unknown Answer Slots"),
    ("full_answer_outcome_tree", 3, "Full-Answer Outcome Tree"),
    ("scope_forbidden_weakening", 3, "Scope and Forbidden Weakening"),
    ("research_policy", 2, "B. Research Policy"),
    ("hard_constraints", 3, "Hard Methodological Constraints"),
    ("soft_preferences", 3, "Soft Research Preferences"),
    ("partial_yields", 3, "Partial Research Yields Worth Preserving"),
    ("structural_diagnosis", 2, "C. Structural Diagnosis"),
    ("detailed_horizon", 2, "D. 第一层：立即启动的 Research Nodes"),
    ("structured_horizon", 2, "E. 第二层：预计随后启动的 Research Directions"),
    ("directional_horizon", 2, "F. 后续研究方向与终局任务"),
)

EDITABLE_KEYS = tuple(
    key for key, _, _ in SECTION_SPECS if key not in {"mathematical_contract", "research_policy"}
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_OLD_TERMS = (
    "Closure Team",
    "Closure Node",
    "Support Node",
    "Integration Node",
    "materializes_into",
    "Proof Workspace",
    "G. Execution Policy and Budget",
)
_EMPTY_STRUCTURED_PATTERNS = (
    "no structured follow-up direction",
    "no structured direction",
    "无需第二层",
    "当前不需要第二层",
    "暂无第二层",
    "尚未成熟到足以形成第二层",
)
_EMPTY_DIRECTIONAL_PATTERNS = (
    "no directional follow-up",
    "no further directional",
    "暂无更远方向",
    "当前无需更远",
)
_REVISION_NARRATIVE_PATTERNS = (
    "上一版",
    "前一版",
    "修订后",
    "相较旧稿",
    "相较上一稿",
    "继续误答旧问题",
    "为了修正上一版",
)
_AGENT_LANGUAGE_PATTERNS = (
    "Answer Obligation",
    "Expected Deliverable",
    "Completion Criterion",
    "False Closure",
    "Major Event",
    "handoff-ready",
    "Promotion",
    "Controller",
    "Worker 分工",
    "可审计含义",
)

_NODE_HEADING_RE = re.compile(
    r"^(?P<label>RN-\d+)\s*(?:[（(]\s*(?P<kind>Discovery|Claim)\s*[）)])\s*[:：.]?\s*(?P<title>.+?)\s*$",
    flags=re.IGNORECASE,
)
_RD_HEADING_RE = re.compile(r"^(?P<label>RD-\d+)\s*[:：.]\s*(?P<title>.+?)\s*$", flags=re.IGNORECASE)
_FRAGMENT_ID_PATTERNS = {
    "detailed_horizon": re.compile(r"^RN-\d+$", flags=re.IGNORECASE),
    "structured_horizon": re.compile(r"^(?:RD|SD)-\d+$", flags=re.IGNORECASE),
}


@dataclass(frozen=True)
class ContractValidation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def repair_message(self) -> str:
        lines = ["本轮 Contract 未通过完整结构校验。请在同一 thread 中一次性修复以下全部问题："]
        lines.extend(f"{index}. {error}" for index, error in enumerate(self.errors, start=1))
        if self.warnings:
            lines.append("\n同时复核以下非阻断警告：")
            lines.extend(f"- {warning}" for warning in self.warnings)
        lines.append("\n请重新提交完整、standalone 的 Research Contract（只包含 A–F），不要返回补丁、差异、版本说明或 G 节运行配置。")
        return "\n".join(lines)


@dataclass(frozen=True)
class ContractDocument:
    title: str
    sections: dict[str, str]
    markdown: str

    def editable_sections(self) -> dict[str, str]:
        return {key: self.sections.get(key, "") for key in EDITABLE_KEYS}

    def with_section(self, key: str, content: str) -> "ContractDocument":
        if key not in EDITABLE_KEYS:
            raise ValueError("该 Contract 章节不可编辑")
        sections = dict(self.sections)
        sections[key] = content.strip()
        markdown = render_contract(self.title, sections)
        return ContractDocument(self.title, sections, markdown)


@dataclass(frozen=True)
class InitialResearchNode:
    label: str
    kind: str
    title: str
    objective: str
    completion_criterion: str
    original_contract_uncertainty_reduced: str
    raw_markdown: str


@dataclass(frozen=True)
class ContractFragment:
    fragment_id: str
    title: str
    body: str
    start: int
    end: int


def parse_contract(markdown: str) -> ContractDocument:
    """Extract fixed A–F sections while preserving deeper mathematical headings."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    title = ""
    sections: dict[str, str] = {}
    spec_by_heading = {(level, heading): key for key, level, heading in SECTION_SPECS}
    current_key: str | None = None
    current_level = 99
    buffers: dict[str, list[str]] = {key: [] for key, _, _ in SECTION_SPECS}

    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            heading = match.group(2).strip()
            if level == 1 and not title:
                title = heading
                current_key = None
                current_level = 1
                continue
            key = spec_by_heading.get((level, heading))
            if key is not None:
                current_key = key
                current_level = level
                continue
            if current_key is not None and level > current_level:
                buffers[current_key].append(line)
                continue
            current_key = None
            current_level = level
            continue
        if current_key is not None:
            buffers[current_key].append(line)

    for key, _, _ in SECTION_SPECS:
        sections[key] = "\n".join(buffers[key]).strip()
    return ContractDocument(title=title, sections=sections, markdown=markdown.rstrip() + "\n")


def render_contract(title: str, sections: dict[str, str]) -> str:
    lines = [f"# {title or TITLE}", ""]
    for key, level, heading in SECTION_SPECS:
        lines.append(f"{'#' * level} {heading}")
        content = sections.get(key, "").strip()
        if content:
            lines.extend(["", content])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(pattern.lower() in lower for pattern in patterns)


def _subheadings(source: str) -> list[tuple[str, str, str, int, int]]:
    """Return (label, kind, title, start, end) for D/E stable fragments."""
    headings = list(re.finditer(r"^###\s+(.+?)\s*$", source, flags=re.MULTILINE))
    result: list[tuple[str, str, str, int, int]] = []
    for index, match in enumerate(headings):
        heading = match.group(1).strip()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(source)
        node = _NODE_HEADING_RE.match(heading)
        if node:
            result.append(
                (
                    node.group("label").upper(),
                    node.group("kind").lower(),
                    node.group("title").strip(),
                    match.start(),
                    end,
                )
            )
            continue
        rd = _RD_HEADING_RE.match(heading)
        if rd:
            result.append((rd.group("label").upper(), "direction", rd.group("title").strip(), match.start(), end))
    return result


def contract_fragments(section_key: str, source: str) -> list[ContractFragment]:
    if section_key not in _FRAGMENT_ID_PATTERNS:
        raise ValueError("只有 D/E Section 支持稳定 Fragment 编辑")
    fragments: list[ContractFragment] = []
    for label, _kind, title, start, end in _subheadings(source):
        if _FRAGMENT_ID_PATTERNS[section_key].fullmatch(label) is None:
            continue
        heading_match = re.match(r"^###\s+.+?\s*$", source[start:end], flags=re.MULTILINE)
        assert heading_match is not None
        body = source[start + heading_match.end():end].strip()
        fragments.append(ContractFragment(label, title, body, start, end))
    return fragments


def validate_fragment_structure(section_key: str, source: str) -> list[str]:
    if section_key not in _FRAGMENT_ID_PATTERNS:
        return []
    headings = list(re.finditer(r"^###\s+(.+?)\s*$", source, flags=re.MULTILINE))
    if section_key == "detailed_horizon" and not headings:
        return ["第一层必须至少保留一个 `### RN-编号（Discovery/Claim）：数学标题` Research Node。"]
    errors: list[str] = []
    seen: set[str] = set()
    for match in headings:
        heading = match.group(1).strip()
        parsed = _NODE_HEADING_RE.match(heading) if section_key == "detailed_horizon" else _RD_HEADING_RE.match(heading)
        if parsed is None:
            expected = "RN-编号（Discovery/Claim）：数学标题" if section_key == "detailed_horizon" else "RD-编号：数学标题"
            errors.append(f"标题 `{heading}` 必须使用 `{expected}`。")
            continue
        label = parsed.group("label").upper()
        title = parsed.group("title").strip()
        if label in seen:
            errors.append(f"稳定 ID `{label}` 重复。")
        seen.add(label)
        if not title:
            errors.append(f"Fragment `{label}` 的数学标题不能为空。")
    return errors


def replace_contract_fragment(section_key: str, source: str, fragment_id: str, title: str, body: str) -> str:
    normalized_id = fragment_id.strip().upper()
    normalized_title = title.strip()
    if not normalized_title:
        raise ValueError("Node/Direction 标题不能为空")
    fragments = contract_fragments(section_key, source)
    matches = [item for item in fragments if item.fragment_id == normalized_id]
    if len(matches) != 1:
        raise ValueError(f"Fragment `{normalized_id}` 不存在或不唯一")
    fragment = matches[0]
    old_heading = re.match(r"^###\s+(.+?)\s*$", source[fragment.start:fragment.end], flags=re.MULTILINE)
    assert old_heading is not None
    old_text = old_heading.group(1).strip()
    node = _NODE_HEADING_RE.match(old_text)
    if section_key == "detailed_horizon":
        if node is None:
            raise ValueError(f"Fragment `{normalized_id}` 缺少 Discovery/Claim 类型")
        heading = f"### {normalized_id}（{node.group('kind').title()}）：{normalized_title}"
    else:
        heading = f"### {normalized_id}：{normalized_title}"
    replacement = heading
    normalized_body = body.strip()
    if normalized_body:
        replacement += f"\n\n{normalized_body}"
    replacement += "\n"
    return source[:fragment.start] + replacement + source[fragment.end:]


def _block_body(body: str, labels: tuple[str, ...]) -> str:
    """Extract a bold block heading with or without a trailing colon."""
    escaped = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(
        rf"^\*\*(?:{escaped})\*\*\s*[:：]?\s*\n+(.*?)(?=^\*\*[^\n]+\*\*\s*[:：]?\s*$|^###\s|\Z)",
        flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(body)
    if match:
        return match.group(1).strip()
    # Backward-compatible old inline field parser.
    for label in labels:
        inline = re.compile(
            rf"^(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*[:：]\s*(.*?)(?=\n(?:\*\*)?[A-Za-z\u4e00-\u9fff][^\n:：]{{0,50}}(?:\*\*)?\s*[:：]|\Z)",
            flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
        ).search(body)
        if inline:
            return inline.group(1).strip()
    return ""


def _heading_blocks(source: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^####\s+(.+?)\s*$", source, flags=re.MULTILINE))
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        blocks.append((match.group(1).strip(), source[match.end():end].strip()))
    return blocks


def validate_contract(document: ContractDocument) -> ContractValidation:
    errors: list[str] = []
    warnings: list[str] = []
    if document.title != TITLE:
        errors.append(f"一级标题必须恰好为 `# {TITLE}`，当前为 `{document.title or '缺失'}`。")

    required_keys = {
        "research_objective",
        "objects_assumptions",
        "unknown_answer_slots",
        "full_answer_outcome_tree",
        "scope_forbidden_weakening",
        "hard_constraints",
        "soft_preferences",
        "partial_yields",
        "structural_diagnosis",
        "detailed_horizon",
    }
    for key, level, heading in SECTION_SPECS:
        if key in required_keys and not document.sections.get(key, "").strip():
            errors.append(f"章节 `{'#' * level} {heading}` 不能为空。")

    unknown = document.sections.get("unknown_answer_slots", "")
    unknown_blocks = _heading_blocks(unknown)
    if unknown_blocks:
        if len(unknown_blocks) > 5:
            warnings.append(f"Unknown Answer Slots 当前有 {len(unknown_blocks)} 个；请检查是否把同一数学未知过度拆分。")
        for title, body in unknown_blocks:
            if len(body) < 120:
                warnings.append(f"Unknown Slot `{title}` 展开较少；请用当前问题中的对象、公式和具体子问题进一步具体化。")

    full_answer = document.sections.get("full_answer_outcome_tree", "")
    if len(full_answer) < 180:
        warnings.append("Full-Answer Outcome Tree 过短；每个主要真相分支应说明什么数学结果组合才算完整。")
    outcome_blocks = _heading_blocks(full_answer)
    if outcome_blocks and len(outcome_blocks) > 4:
        warnings.append(f"Full-Answer Outcome Tree 当前有 {len(outcome_blocks)} 个分支；请检查是否可以合并为 2–4 个主要真相分支。")

    structural = document.sections.get("structural_diagnosis", "")
    if len(structural) < 240:
        warnings.append("Structural Diagnosis 较短；请确认 Contractor 已做实际数学诊断，而不只是复述题面。")

    detailed = document.sections.get("detailed_horizon", "")
    errors.extend(validate_fragment_structure("detailed_horizon", detailed))
    node_headings = list(re.finditer(r"^###\s+(.+?)\s*$", detailed, flags=re.MULTILINE))
    if not node_headings:
        errors.append("第一层必须至少包含一个 Research Node。")
    if len(node_headings) > 5:
        warnings.append(f"第一层当前有 {len(node_headings)} 个 Nodes；通常应控制在 2–5 个，除非确有多个独立工作流。")
    for index, match in enumerate(node_headings):
        end = node_headings[index + 1].start() if index + 1 < len(node_headings) else len(detailed)
        heading = match.group(1).strip()
        body = detailed[match.end():end].strip()
        parsed = _NODE_HEADING_RE.match(heading)
        if parsed is None:
            continue
        label = parsed.group("label").upper()
        required_blocks = {
            "和原问题的关系": _block_body(body, ("和原问题的关系", "Relation to the Original Problem")),
            "当前目标": _block_body(body, ("当前目标", "Current Goal", "Objective")),
            "当前输入": _block_body(body, ("当前输入", "Current Inputs")),
            "首轮方式": _block_body(body, ("首轮方式", "First-Round Approach")),
        }
        for block_name, value in required_blocks.items():
            if not value:
                errors.append(f"第一层节点 `{label}` 必须包含 `**{block_name}**`。")
        if required_blocks["当前目标"] and len(required_blocks["当前目标"]) < 80:
            warnings.append(f"第一层节点 `{label}` 的“当前目标”较短；应给出具体对象、公式、量词或候选 statement。")
        if required_blocks["首轮方式"] and len(required_blocks["首轮方式"]) < 80:
            warnings.append(f"第一层节点 `{label}` 的“首轮方式”较短；应说明具体数学动作和分叉依据。")

    structured = document.sections.get("structured_horizon", "").strip()
    if not structured:
        errors.append("第二层可以没有方向，但必须明确说明当前为什么不需要 Structured follow-up。")
    elif _contains_any(structured, _EMPTY_STRUCTURED_PATTERNS):
        pass
    else:
        errors.extend(validate_fragment_structure("structured_horizon", structured))
        rd_headings = list(re.finditer(r"^###\s+(.+?)\s*$", structured, flags=re.MULTILINE))
        if not rd_headings:
            errors.append("第二层若存在方向，必须使用 `### RD-编号：数学标题`。")
        if re.search(r"\bRN-\d+\b", structured, flags=re.IGNORECASE):
            errors.append("第二层触发条件必须自包含，不能用 `RN-编号` 代替数学条件。")
        for index, match in enumerate(rd_headings):
            end = rd_headings[index + 1].start() if index + 1 < len(rd_headings) else len(structured)
            heading = match.group(1).strip()
            parsed = _RD_HEADING_RE.match(heading)
            if parsed is None:
                continue
            label = parsed.group("label").upper()
            body = structured[match.end():end].strip()
            blocks = {
                "和原问题的关系": _block_body(body, ("和原问题的关系", "Relation to the Original Problem")),
                "触发条件": _block_body(body, ("触发条件", "Activation Condition")),
                "大致目标与路径": _block_body(body, ("大致目标与路径", "Goal and Possible Route")),
            }
            for block_name, value in blocks.items():
                if not value:
                    errors.append(f"第二层方向 `{label}` 必须包含 `**{block_name}**`。")
            if blocks["大致目标与路径"] and len(blocks["大致目标与路径"]) < 80:
                warnings.append(f"第二层方向 `{label}` 的“大致目标与路径”较短；请进一步数学化展开。")

    directional = document.sections.get("directional_horizon", "").strip()
    if not directional:
        errors.append("F 节可以很短，但必须说明远期数学方向或为什么当前无需更远规划。")
    elif not _contains_any(directional, _EMPTY_DIRECTIONAL_PATTERNS):
        f_headings = list(re.finditer(r"^###\s+(.+?)\s*$", directional, flags=re.MULTILINE))
        if not f_headings:
            warnings.append("F 节建议用 3–4 个三级标题组织远期数学方向。")
        elif len(f_headings) > 4:
            warnings.append(f"F 节当前有 {len(f_headings)} 个方向；请检查是否可以合并为 3–4 个自然 horizon。")
        if re.search(r"\bRN-\d+\b", directional, flags=re.IGNORECASE):
            warnings.append("F 节不应依赖 RN 编号表达语义；请用自包含数学语言说明。")
        for index, match in enumerate(f_headings):
            end = f_headings[index + 1].start() if index + 1 < len(f_headings) else len(directional)
            body = directional[match.end():end].strip()
            relation = _block_body(body, ("和原问题的关系", "Relation to the Original Problem"))
            if not relation:
                errors.append(f"F 节方向 `{match.group(1).strip()}` 必须包含 `**和原问题的关系**`。")

    if re.search(r"^##\s+G\.\s*Execution Policy", document.markdown, flags=re.MULTILINE | re.IGNORECASE):
        errors.append("Execution Policy 必须由用户在结构化 G 表单中选择，不得出现在 Agent 生成的 Contract Markdown。")

    for term in _OLD_TERMS:
        if term.lower() in document.markdown.lower():
            errors.append(f"检测到旧架构术语 `{term}`；用户可见 Contract 不得带入旧 Proof-DAG 语义。")
    for term in _AGENT_LANGUAGE_PATTERNS:
        if term.lower() in document.markdown.lower():
            errors.append(f"检测到用户可见的系统/审计语言 `{term}`；必须改写成自然数学语言。")
    for term in _REVISION_NARRATIVE_PATTERNS:
        if term in document.markdown:
            errors.append(f"检测到版本修订叙事 `{term}`；当前 Contract 必须作为 standalone snapshot 重写。")
    if re.search(r"大规模数值|训练\s*\d+\s*次|运行模拟|Monte\s*Carlo", document.markdown, flags=re.IGNORECASE):
        warnings.append("当前产品仅允许短时 exact/symbolic sanity check，不允许把大规模数值模拟规划为本轮数学证据。")
    return ContractValidation(errors, warnings)


def contract_payload(document: ContractDocument) -> dict[str, Any]:
    return {"title": document.title, "sections": document.sections}


def extract_initial_nodes(document: ContractDocument) -> list[InitialResearchNode]:
    text = document.sections.get("detailed_horizon", "")
    headings = list(re.finditer(r"^###\s+(.+?)\s*$", text, flags=re.MULTILINE))
    nodes: list[InitialResearchNode] = []
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        heading = match.group(1).strip()
        parsed = _NODE_HEADING_RE.match(heading)
        if parsed is None:
            continue
        body = text[match.end():end].strip()
        relation = _block_body(body, ("和原问题的关系", "Relation to the Original Problem"))
        objective = _block_body(body, ("当前目标", "Current Goal", "Objective")) or body[:1200]
        first_round = _block_body(body, ("首轮方式", "First-Round Approach"))
        completion = (
            "由 Node Supervisor 根据本节点的‘当前目标’和‘和原问题的关系’在首轮初始化时正规化。"
            "完成标准必须能够判断当前目标中的数学问题是否已形成可靠结论，并说明其对原问题的推进。"
        )
        if first_round:
            completion += f" 首轮计划为：{first_round[:500]}"
        nodes.append(
            InitialResearchNode(
                label=parsed.group("label").upper(),
                kind=parsed.group("kind").lower(),
                title=parsed.group("title").strip(),
                objective=objective.strip(),
                completion_criterion=completion,
                original_contract_uncertainty_reduced=relation.strip(),
                raw_markdown=f"### {heading}\n\n{body}\n",
            )
        )
    return nodes
