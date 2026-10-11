from __future__ import annotations

from dataclasses import dataclass, field
import re

PERSONAS = ("literature", "construction", "general_proof", "cross_domain")
FINDING_TYPES = {
    "candidate_answer", "possible_claim", "near_counterexample", "proof_mechanism",
    "mechanism", "structural_obstruction", "parameter_regime", "unexpected_reduction",
    "missing_bridge", "new_question",
}
ALLOWED_ACTIONS = {
    "CONTINUE", "SPAWN_DISCOVERY_NODE", "SPAWN_CLAIM_NODE", "MERGE_NODES",
    "RETIRE", "REQUEST_FRESH_VERIFICATION", "COMPLETE_RUN",
}
ALLOWED_COMPLETION_KINDS = {
    "fully_resolved", "partially_resolved", "budget_exhausted", "structurally_blocked",
}


@dataclass(frozen=True)
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    format_name: str = "Markdown"

    @property
    def ok(self) -> bool:
        return not self.errors

    def repair_message(self) -> str:
        lines = [f"本轮业务 {self.format_name} 未通过完整契约校验。请一次性修复以下全部问题："]
        lines.extend(f"{i}. {item}" for i, item in enumerate(self.errors, 1))
        if self.warnings:
            lines.append("\n并请复核以下警告：")
            lines.extend(f"- {item}" for item in self.warnings)
        lines.append(f"\n请重新提交完整 {self.format_name}，不要只给补丁；数学内容应尽量保留。")
        return "\n".join(lines)


@dataclass(frozen=True)
class WorkerAssignmentSpec:
    worker_label: str
    persona: str
    task: str


@dataclass(frozen=True)
class FindingSpec:
    title: str
    finding_type: str
    significance: str
    markdown: str
    nominate: bool
    rationale: str
    recommended_actions: list[str]


@dataclass(frozen=True)
class ArchitectureCommunicationSpec:
    title: str
    attention: str
    question: str
    global_consequence: str
    affected_node_ids: tuple[str, ...]
    related_evidence_ids: tuple[str, ...]
    local_pressure_tests: str
    rationale: str


@dataclass(frozen=True)
class SupervisorDecision:
    diagnosis: str
    assignments: list[WorkerAssignmentSpec]
    retire_worker_labels: list[str]
    findings: list[FindingSpec]
    node_status: str
    major_event: bool
    claim_candidate: str | None
    supervisor_notes: str
    raw_markdown: str
    communications: list[ArchitectureCommunicationSpec] = field(default_factory=list)
    architecture_attention: str = "none"
    completion_assessment: str = ""


@dataclass(frozen=True)
class WorkerReport:
    status: str
    major_event: bool
    major_event_type: str | None
    raw_markdown: str


@dataclass(frozen=True)
class ArchitectureAction:
    action_type: str
    target: str
    reason: str
    supervisor_directive: str = ""
    title: str = ""
    node_kind: str = ""
    objective: str = ""
    completion_criterion: str = ""
    relation: str = ""
    condition: str = ""
    finding_id: str = ""
    evidence_id: str = ""
    candidate_artifact_id: str = ""
    origin_direction: str = ""
    activation_evidence: str = ""
    source_node_ids: tuple[str, ...] = ()
    completion_kind: str = ""
    is_final_claim: bool = False
    original_contract_uncertainty_reduced: str = ""


@dataclass(frozen=True)
class PromotionDecisionSpec:
    proposal_id: str
    decision: str
    reason: str


@dataclass(frozen=True)
class ArchitectureDecision:
    diagnosis: str
    actions: list[ArchitectureAction]
    raw_markdown: str
    evidence_inspected: str = ""
    promotion_decisions: list[PromotionDecisionSpec] = field(default_factory=list)
    snapshot_update: str = ""
    ledger_updates: str = ""
    deferred_findings: str = ""
    legacy_schema: bool = False


@dataclass(frozen=True)
class VerificationDecision:
    verdict: str
    statement: str
    decisive_issues: str
    raw_markdown: str
    reuse_boundary: str = ""
    verified_result: str = ""


@dataclass(frozen=True)
class CandidateIdentity:
    """Canonical mathematical identity extracted from one immutable Candidate."""

    statement: str
    proof: str
    scope: str
    controlling_proposition_id: str


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_SUBSECTION_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)


def _section(markdown: str, heading: str) -> str:
    matches = list(_SECTION_RE.finditer(markdown))
    for index, match in enumerate(matches):
        if _heading_key(match.group(1)) != _heading_key(heading):
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        return markdown[start:end].strip()
    return ""


def markdown_section(markdown: str, heading: str) -> str:
    """按宽松标题匹配读取用户可见二级 Markdown 章节。"""
    return _section(markdown, heading)


def _heading_key(value: str) -> str:
    """忽略英文大小写和多余空白，但不把不同业务标题混为一谈。"""
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _subsections(text: str) -> list[tuple[str, str]]:
    matches = list(_SUBSECTION_RE.finditer(text))
    result: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result.append((match.group(1).strip(), text[start:end].strip()))
    return result


def _nested_section(markdown: str, *headings: str) -> str:
    """Read one level-2/3 natural Markdown section without changing its body."""
    matches = list(re.finditer(r"^#{2,3}\s+(.+?)\s*$", markdown, flags=re.MULTILINE))
    accepted = {item.strip().casefold() for item in headings}
    for index, match in enumerate(matches):
        if match.group(1).strip().casefold() not in accepted:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        return markdown[start:end].strip()
    return ""


def candidate_statement_self_containment_errors(statement: str) -> list[str]:
    """Reject only explicit unresolved references that cannot survive cold-start reuse."""
    errors: list[str] = []
    lowered = statement.casefold()
    referential_phrases = (
        "上述设定", "上述假设", "上述条件", "前述设定", "前述假设", "前述条件",
        "沿用前文", "沿用上述", "如上所述", "见上文", "as above",
        "above assumptions", "above conditions", "aforementioned assumptions",
        "previous assumptions", "previous notation",
    )
    if any(phrase in lowered for phrase in referential_phrases):
        errors.append("精确陈述含有无法 cold-start 解析的前文/上述引用")
    if re.search(r"(?:条件|假设|设定|assumptions?|conditions?)\s*(?:S[_-]?\d)(?:\s*[,，、/]\s*S[_-]?\d)*", statement, flags=re.IGNORECASE):
        errors.append("精确陈述引用 S1/S2 等未展开条件标签")
    if re.search(r"\[(?:在此|此处|补充|TODO|待补|省略|insert|fill)[^\]]*\]", statement, flags=re.IGNORECASE):
        errors.append("精确陈述仍含占位文本")
    return errors


def parse_candidate_identity(
    markdown: str,
    *,
    node_kind: str,
    controlling_proposition_id: str,
) -> tuple[CandidateIdentity | None, ValidationResult]:
    """Extract Candidate statement/proof and enforce explicit auxiliary/controlling scope."""
    errors: list[str] = []
    statement = _nested_section(markdown, "精确陈述", "Exact Statement")
    proof = _nested_section(markdown, "完整论证", "Complete Argument")
    scope_block = _nested_section(markdown, "Candidate Scope", "候选范围")
    proposition_block = _nested_section(
        markdown, "Controlling Proposition ID", "控制命题 ID"
    )
    scope_match = re.search(
        r"\b(controlling_node_claim|auxiliary_fact)\b", scope_block, flags=re.IGNORECASE
    )
    if "当前节点控制命题" in scope_block:
        scope = "controlling_node_claim"
    elif "辅助事实" in scope_block:
        scope = "auxiliary_fact"
    else:
        scope = scope_match.group(1).lower() if scope_match else ""
    proposition_id = _proposition_identifier(proposition_block) if proposition_block else ""
    if not statement:
        errors.append("Claim Candidate 的 `精确陈述` 不能为空")
    if not proof:
        errors.append("Claim Candidate 的 `完整论证` 不能为空")
    if not scope:
        errors.append("Claim Candidate 必须声明 Candidate Scope：辅助事实 或 当前节点控制命题")
    if scope == "controlling_node_claim":
        if node_kind != "claim":
            errors.append("Discovery Node 的 Candidate 只能是 auxiliary_fact")
        if proposition_id != controlling_proposition_id:
            errors.append("controlling Candidate 必须唯一绑定当前 Controlling Proposition ID")
    elif proposition_id:
        errors.append("auxiliary_fact 不得声明 Controlling Proposition ID")
    if statement:
        errors.extend(candidate_statement_self_containment_errors(statement))
    validation = ValidationResult(errors, [], format_name="Claim Candidate Markdown")
    if not validation.ok:
        return None, validation
    return CandidateIdentity(
        statement=statement,
        proof=proof,
        scope=scope,
        controlling_proposition_id=proposition_id,
    ), validation


def _field(body: str, name: str) -> str:
    match = re.search(
        rf"^(?:\*\*)?{re.escape(name)}(?:\*\*)?[ \t]*[:：][ \t]*(.*?)[ \t]*$",
        body, flags=re.MULTILINE | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _identifier(value: str) -> str:
    """读取字段首个非空机器标识符，并忽略常见 Markdown 包裹。"""
    cleaned = value.strip()
    fenced = re.fullmatch(
        r"```(?:text|markdown)?\s*(.*?)\s*```", cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        cleaned = fenced.group(1).strip()
    cleaned = next((line.strip() for line in cleaned.splitlines() if line.strip()), "")
    match = re.fullmatch(r"`([^`\r\n]+)`", cleaned)
    return match.group(1).strip() if match else cleaned


def _machine_identifier(value: str) -> str:
    """只接受一个完整机器 ID，拒绝在同一字段混入说明或第二个标识符。"""
    cleaned = _identifier(value)
    return cleaned if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", cleaned) else ""


def _proposition_identifier(value: str) -> str:
    """从说明文字或 Markdown 包裹中提取唯一 controlling proposition ID。"""
    matches = set(re.findall(r"claim-proposition:[A-Za-z0-9_-]+", value))
    if len(matches) == 1:
        return next(iter(matches))
    cleaned = _identifier(value)
    return cleaned if re.fullmatch(r"claim-proposition:[A-Za-z0-9_-]+", cleaned) else ""


def _enum_value(value: str, allowed: set[str]) -> str:
    """从带说明的字段中提取唯一合法英文枚举，歧义时返回空值。"""
    normalized = _identifier(value).upper()
    matches = [
        item for item in allowed
        if re.search(
            rf"(?<![A-Z0-9_]){re.escape(item.upper())}(?![A-Z0-9_])",
            normalized,
        )
    ]
    return matches[0] if len(matches) == 1 else ""


def _field_block(body: str, name: str) -> str:
    match = re.search(
        rf"^(?:\*\*)?{re.escape(name)}(?:\*\*)?[ \t]*[:：][ \t]*(.*?)(?=\n(?:\*\*)?[A-Za-z\u4e00-\u9fff][^\n:：]{{0,80}}(?:\*\*)?[ \t]*[:：]|\Z)",
        body, flags=re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _none(text: str) -> bool:
    return text.strip().lower() in {"", "无", "无。", "暂无", "暂无。", "none", "n/a"}


def _has_section(markdown: str, heading: str) -> bool:
    expected = _heading_key(heading)
    return any(_heading_key(match.group(1)) == expected for match in _SECTION_RE.finditer(markdown))


def validate_supervisor_candidate(
    markdown: str,
    *,
    node_kind: str,
    controlling_proposition_id: str,
) -> ValidationResult:
    """即使 Supervisor 外层格式有错，也独立收集 Candidate 的全部错误。"""
    candidate = _section(markdown, "Claim Candidate")
    if not candidate:
        return ValidationResult([], [], format_name="Claim Candidate Markdown")
    _, validation = parse_candidate_identity(
        candidate,
        node_kind=node_kind,
        controlling_proposition_id=controlling_proposition_id,
    )
    return validation


def parse_supervisor(markdown: str, *, bootstrap: bool, max_workers: int) -> tuple[SupervisorDecision | None, ValidationResult]:
    errors: list[str] = []
    warnings: list[str] = []
    diagnosis = _section(markdown, "Current Diagnosis")
    assignments_text = _section(markdown, "Worker Assignments")
    retirements_text = _section(markdown, "Worker Retirements")
    findings_text = _section(markdown, "Research Findings")
    communications_text = _section(markdown, "Architecture Communications")
    decision_text = _section(markdown, "Node Decision")
    candidate = _section(markdown, "Claim Candidate") or None
    notes = _section(markdown, "Supervisor Notes")
    completion_assessment = _section(markdown, "Discovery Completion Assessment")
    new_schema = _has_section(markdown, "Architecture Communications") or bool(
        re.search(r"^架构关注\s*[:：]", decision_text, flags=re.MULTILINE)
    )

    if not diagnosis:
        errors.append("缺少或留空 `## Current Diagnosis`")
    if not assignments_text:
        errors.append("缺少 `## Worker Assignments`（没有新任务时写 `无。`）")

    assignments: list[WorkerAssignmentSpec] = []
    labels: set[str] = set()
    if not _none(assignments_text):
        for label, body in _subsections(assignments_text):
            persona = _field(body, "Persona").lower().replace("-", "_")
            task = _field_block(body, "Task") or _field(body, "Task")
            if persona not in PERSONAS:
                errors.append(f"Worker `{label}` 的 Persona 非法：`{persona or '缺失'}`")
            if not task:
                errors.append(f"Worker `{label}` 缺少完整 Task")
            if label in labels:
                errors.append(f"Worker label 重复：{label}")
            labels.add(label)
            if persona in PERSONAS and task:
                assignments.append(WorkerAssignmentSpec(label, persona, task))
    if len(assignments) > max_workers:
        errors.append(f"本轮立即执行任务 {len(assignments)} 个，超过 Worker 硬上限 {max_workers}")
    if bootstrap:
        personas = {item.persona for item in assignments}
        missing = [persona for persona in PERSONAS if persona not in personas]
        if missing:
            errors.append("首轮初始化必须覆盖全部四种 Persona，缺少：" + "、".join(missing))

    retire_worker_labels: list[str] = []
    if retirements_text and not _none(retirements_text):
        for raw in retirements_text.splitlines():
            value = raw.lstrip("-* ").strip()
            if value:
                retire_worker_labels.append(value)

    findings: list[FindingSpec] = []
    if findings_text and not _none(findings_text):
        for title, body in _subsections(findings_text):
            finding_type = (_field(body, "Type") or _field(body, "类型")).lower().replace(" ", "_")
            significance = (
                _field(body, "Mathematical Significance")
                or _field(body, "Significance")
                or _field(body, "重要性")
                or "normal"
            ).lower()
            nomination = (
                _field(body, "Promotion Nomination")
                or _field(body, "Nomination")
                or _field(body, "提名")
                or "no"
            ).lower()
            rationale = _field_block(body, "Rationale") or _field_block(body, "提名理由")
            recommended = _field_block(body, "Recommended Actions") or _field_block(body, "建议动作")
            if finding_type not in FINDING_TYPES:
                warnings.append(f"Finding `{title}` 使用未登记标签 `{finding_type}`，按 new_question 保存。")
                finding_type = "new_question"
            if significance not in {"normal", "substantial", "major"}:
                errors.append(f"Finding `{title}` 的 Mathematical Significance 非法：`{significance}`")
            nominate = nomination in {"yes", "true", "是", "nominate", "promotion"}
            if new_schema and nomination not in {"yes", "no"}:
                errors.append(f"Finding `{title}` 的 Promotion Nomination 必须是 yes 或 no")
            if nominate and not rationale:
                errors.append(f"被提名 Finding `{title}` 缺少 Rationale")
            findings.append(FindingSpec(
                title=title, finding_type=finding_type, significance=significance,
                markdown=body, nominate=nominate, rationale=rationale,
                recommended_actions=[line.lstrip("-* ").strip() for line in recommended.splitlines() if line.strip()],
            ))

    communications: list[ArchitectureCommunicationSpec] = []
    if new_schema and not _has_section(markdown, "Architecture Communications"):
        errors.append("缺少 `## Architecture Communications`（没有时写 `暂无。`）")
    if communications_text and not _none(communications_text):
        for title, body in _subsections(communications_text):
            attention = _field(body, "Attention").lower()
            question = _field_block(body, "Question") or _field(body, "Question")
            consequence = _field_block(body, "Global Consequence") or _field(body, "Global Consequence")
            affected = _field(body, "Affected Nodes")
            related = _field(body, "Related Findings or Evidence")
            pressure = _field_block(body, "Local Pressure Tests") or _field(body, "Local Pressure Tests")
            rationale = _field_block(body, "Rationale") or _field(body, "Rationale")
            if attention not in {"next_review", "urgent"}:
                errors.append(f"Communication `{title}` 的 Attention 必须是 next_review 或 urgent")
            if not question or not consequence or not pressure or not rationale:
                errors.append(
                    f"Communication `{title}` 必须提供 Question、Global Consequence、Local Pressure Tests 与 Rationale"
                )
            communications.append(ArchitectureCommunicationSpec(
                title=title,
                attention=attention,
                question=question,
                global_consequence=consequence,
                affected_node_ids=tuple(
                    part for part in re.split(r"[,，\s]+", affected.strip()) if part and part.lower() not in {"无", "none"}
                ),
                related_evidence_ids=tuple(
                    part for part in re.split(r"[,，\s]+", related.strip()) if part and part.lower() not in {"无", "none"}
                ),
                local_pressure_tests=pressure,
                rationale=rationale,
            ))

    status_match = re.search(r"^.*?状态\s*[:：]?\s*(.+?)\s*$", decision_text, flags=re.MULTILINE)
    attention_match = re.search(r"^架构关注\s*[:：]\s*(.+?)\s*$", decision_text, flags=re.MULTILINE)
    event_match = re.search(r"^重大事件\s*[:：]\s*(.+?)\s*$", decision_text, flags=re.MULTILINE)
    allowed_status = {"继续研究", "候选已形成", "节点完成", "进入收束", "阻塞"}
    status_text = status_match.group(1).strip() if status_match else ""
    matched_statuses = [item for item in allowed_status if item in status_text]
    node_status = matched_statuses[0] if len(matched_statuses) == 1 else ""
    if node_status not in allowed_status:
        errors.append("Node Decision 状态必须是：继续研究、候选已形成、节点完成、进入收束或阻塞")
    attention_labels = {"无": "none", "下次审查": "next_review", "紧急": "urgent"}
    architecture_attention = attention_labels.get(
        attention_match.group(1).strip() if attention_match else "", ""
    )
    if new_schema and not architecture_attention:
        errors.append("Node Decision 架构关注必须是：无、下次审查或紧急")
    communication_attention = (
        "urgent" if any(item.attention == "urgent" for item in communications)
        else "next_review" if communications else "none"
    )
    if new_schema and architecture_attention and architecture_attention != communication_attention:
        errors.append("Node Decision 的架构关注必须等于 Architecture Communications 的最高 Attention")
    major_event = bool(
        not new_schema
        and event_match
        and event_match.group(1).strip().lower() in {"是", "yes", "true"}
    )
    if not new_schema:
        architecture_attention = "urgent" if major_event else "none"
    if node_status == "候选已形成" and not candidate:
        errors.append("状态为“候选已形成”时必须提供完整 `## Claim Candidate`")
    if node_status == "节点完成" and not completion_assessment:
        errors.append("状态为“节点完成”时必须提供非空 `## Discovery Completion Assessment`")
    if candidate and "精确陈述" not in candidate and "Exact Statement" not in candidate:
        errors.append("Claim Candidate 必须包含完整精确陈述")
    if candidate and "完整论证" not in candidate and "Complete Argument" not in candidate:
        errors.append("Claim Candidate 必须包含自包含完整论证")

    validation = ValidationResult(errors, warnings)
    if not validation.ok:
        return None, validation
    return SupervisorDecision(
        diagnosis=diagnosis, assignments=assignments,
        retire_worker_labels=retire_worker_labels, findings=findings,
        node_status=node_status, major_event=major_event,
        claim_candidate=candidate, supervisor_notes=notes,
        raw_markdown=markdown,
        communications=communications,
        architecture_attention=architecture_attention,
        completion_assessment=completion_assessment,
    ), validation


def parse_worker_report(markdown: str) -> tuple[WorkerReport | None, ValidationResult]:
    errors: list[str] = []
    matches = [
        match for match in _SECTION_RE.finditer(markdown)
        if _heading_key(match.group(1)) == _heading_key("任务状态")
    ]
    if len(matches) != 1:
        errors.append("报告必须包含且只包含一个 `## 任务状态`")
    status = ""
    if matches:
        tail = markdown[matches[0].end():]
        next_heading = re.search(r"^##\s+", tail, flags=re.MULTILINE)
        block = tail[:next_heading.start()] if next_heading else tail
        status_line = next(
            (line.strip() for line in block.splitlines() if "状态" in line),
            next((line.strip() for line in block.splitlines() if line.strip()), ""),
        )
        states = {
            item for item in ("已完成", "未完成")
            if re.search(re.escape(item), status_line)
        }
        if len(states) != 1:
            errors.append("任务状态必须为 `状态：已完成` 或 `状态：未完成`")
        else:
            status = "completed" if next(iter(states)) == "已完成" else "partial"
    major_block = _section(markdown, "重大事件")
    major_type: str | None = None
    major = False
    if major_block:
        match = re.search(r"^(?:类型\s*[:：]\s*)?(.+?)\s*$", major_block, flags=re.MULTILINE)
        if match:
            major_type = match.group(1).strip()
            major = major_type.lower() not in {"无", "无。", "none", "暂无"}
    if len(markdown.strip()) < 80:
        errors.append("研究报告过短，不能只提交状态字段")
    validation = ValidationResult(errors, [])
    if not validation.ok:
        return None, validation
    return WorkerReport(status, major, major_type, markdown), validation


def parse_architecture_review(markdown: str) -> tuple[ArchitectureDecision | None, ValidationResult]:
    errors: list[str] = []
    warnings: list[str] = []
    diagnosis = _section(markdown, "Global Diagnosis")
    actions_text = _section(markdown, "Actions")
    evidence_inspected = _section(markdown, "Evidence Inspected On Demand")
    promotions_text = _section(markdown, "Promotion Decisions")
    snapshot_update = _section(markdown, "Architecture Snapshot Update")
    ledger_updates = _section(markdown, "Decision Ledger Updates")
    deferred_findings = _section(markdown, "Deferred Findings")
    new_schema = _has_section(markdown, "Architecture Snapshot Update") or markdown.lstrip().startswith(
        "# Fresh Research Architecture Review"
    )
    if not diagnosis:
        errors.append("缺少 `## Global Diagnosis`")
    if not actions_text:
        errors.append("缺少 `## Actions`")
    if new_schema:
        for heading in (
            "Evidence Inspected On Demand", "Promotion Decisions",
            "Architecture Snapshot Update", "Decision Ledger Updates", "Deferred Findings",
        ):
            if not _has_section(markdown, heading):
                errors.append(f"缺少 `## {heading}`")

    actions: list[ArchitectureAction] = []
    for label, body in ([] if _none(actions_text) else _subsections(actions_text)):
        action_errors: list[str] = []
        action_type = _enum_value(_field(body, "Type"), ALLOWED_ACTIONS)
        target = _identifier(_field(body, "Target"))
        reason = _field_block(body, "Reason") or _field(body, "Reason")
        if action_type not in ALLOWED_ACTIONS:
            action_errors.append(f"Type 非法：{_field(body, 'Type') or '缺失'}")
        if not target:
            action_errors.append("缺少 Target")
        if not reason:
            action_errors.append("缺少 Reason")
        title = _field(body, "Title")
        node_kind = _enum_value(_field(body, "Node Kind"), {"discovery", "claim"}).lower()
        objective = _field_block(body, "Objective") or _field(body, "Objective")
        completion = _field_block(body, "Completion Criterion") or _field(body, "Completion Criterion")
        relation_raw = _field(body, "Relation")
        relation = _enum_value(
            relation_raw,
            {"requires", "informs", "alternative_to", "conditional_on"},
        ).lower()
        condition = _field_block(body, "Condition") or _field(body, "Condition")
        finding_id = _identifier(_field(body, "Finding ID"))
        evidence_raw = _field(body, "Evidence ID")
        evidence_id = _machine_identifier(evidence_raw)
        if evidence_raw and not evidence_id:
            action_errors.append("Evidence ID 必须且只能包含一个完整机器 ID")
        candidate_artifact_id = _identifier(_field(body, "Candidate Artifact ID"))
        origin_direction = _field(body, "Origin Direction or Proposal") or _field(body, "Origin Direction")
        activation_evidence = _field_block(body, "Activation Evidence") or _field(body, "Activation Evidence")
        uncertainty_reduced = (
            _field_block(body, "Original-Contract Uncertainty Reduced")
            or _field(body, "Original-Contract Uncertainty Reduced")
        )
        supervisor_directive = _field_block(body, "Supervisor Directive") or _field(body, "Supervisor Directive")
        completion_kind_raw = _field(body, "Completion Kind")
        completion_kind = _enum_value(
            completion_kind_raw, ALLOWED_COMPLETION_KINDS
        ).lower()
        final_claim_raw = _field(body, "Final Claim").lower()
        is_final_claim = final_claim_raw in {"yes", "true", "是", "1"}
        source_nodes_raw = _field(body, "Source Nodes")
        source_nodes = tuple(
            _identifier(part)
            for part in re.split(r"[,，\s]+", source_nodes_raw)
            if part.strip()
        )
        if action_type in {"SPAWN_DISCOVERY_NODE", "SPAWN_CLAIM_NODE"}:
            expected = "discovery" if action_type == "SPAWN_DISCOVERY_NODE" else "claim"
            if node_kind and node_kind != expected:
                action_errors.append("Node Kind 与 Type 不一致")
            node_kind = expected
            if not title or not objective or not completion:
                action_errors.append("创建 Node 时必须给出 Title、Objective、Completion Criterion")
            if new_schema and (not activation_evidence or not uncertainty_reduced):
                action_errors.append(
                    "创建 Node 时必须给出 Activation Evidence 与 Original-Contract Uncertainty Reduced"
                )
        if action_type == "CONTINUE" and not supervisor_directive:
            warnings.append(f"{label} 的 CONTINUE 没有 Supervisor Directive；将只保持当前 Node")
        if action_type == "REQUEST_FRESH_VERIFICATION" and not candidate_artifact_id:
            action_errors.append("请求 fresh verification 时必须给出 Candidate Artifact ID")
        if action_type == "MERGE_NODES":
            if len(source_nodes) < 2:
                action_errors.append("MERGE_NODES 必须给出至少两个 Source Nodes")
            if node_kind not in {"discovery", "claim"}:
                action_errors.append("MERGE_NODES 必须给出 Node Kind: discovery/claim")
            if not title or not objective or not completion:
                action_errors.append("MERGE_NODES 必须给出新节点 Title、Objective、Completion Criterion")
            if new_schema and not uncertainty_reduced:
                action_errors.append("MERGE_NODES 必须给出 Original-Contract Uncertainty Reduced")
        if action_type == "COMPLETE_RUN":
            if completion_kind not in ALLOWED_COMPLETION_KINDS:
                action_errors.append("Completion Kind 非法")
            if completion_kind == "fully_resolved" and not evidence_id:
                action_errors.append("fully_resolved 必须绑定 controlling Evidence ID")
        if relation_raw and not relation:
            action_errors.append(f"使用非法 Relation：{relation_raw}")
        if action_errors:
            warnings.extend(f"Action `{label}` 已忽略：{item}" for item in action_errors)
            continue
        actions.append(ArchitectureAction(
            action_type=action_type, target=target, reason=reason,
            supervisor_directive=supervisor_directive, title=title,
            node_kind=node_kind, objective=objective,
            completion_criterion=completion, relation=relation,
            condition=condition, finding_id=finding_id,
            evidence_id=evidence_id, candidate_artifact_id=candidate_artifact_id,
            origin_direction=origin_direction, activation_evidence=activation_evidence,
            source_node_ids=source_nodes, completion_kind=completion_kind,
            is_final_claim=is_final_claim,
            original_contract_uncertainty_reduced=uncertainty_reduced,
        ))
    if not actions and not new_schema:
        errors.append("Architecture Review 至少需要一个 Action；无变化时使用 CONTINUE")

    promotion_decisions: list[PromotionDecisionSpec] = []
    if promotions_text and not _none(promotions_text):
        for proposal_id, body in _subsections(promotions_text):
            decision = _field(body, "Decision").lower()
            reason = _field_block(body, "Reason") or _field(body, "Reason")
            if decision:
                if decision not in {"accept", "return_local", "defer", "reject", "merge"}:
                    errors.append(f"Promotion `{proposal_id}` 的 Decision 非法：`{decision}`")
                if not reason:
                    errors.append(f"Promotion `{proposal_id}` 缺少 Reason")
                promotion_decisions.append(PromotionDecisionSpec(proposal_id, decision, reason))
    validation = ValidationResult(errors, warnings)
    if not validation.ok:
        return None, validation
    return ArchitectureDecision(
        diagnosis, actions, markdown,
        evidence_inspected=evidence_inspected,
        promotion_decisions=promotion_decisions,
        snapshot_update=snapshot_update,
        ledger_updates=ledger_updates,
        deferred_findings=deferred_findings,
        legacy_schema=not new_schema,
    ), validation


def parse_verification(markdown: str) -> tuple[VerificationDecision | None, ValidationResult]:
    errors: list[str] = []
    verdict_block = _section(markdown, "Verdict")
    verdicts = set(re.findall(r"\b(PASS|REJECT)\b", verdict_block, flags=re.IGNORECASE))
    verdicts = {item.upper() for item in verdicts}
    verdict = next(iter(verdicts)) if len(verdicts) == 1 else ""
    if not verdict:
        errors.append("Verifier Verdict 必须且只能表达 PASS 或 REJECT 中的一个")
    statement = _section(markdown, "Exact Statement")
    if not statement:
        errors.append("缺少 `## Exact Statement`")
    verified_result = _section(markdown, "Verified Mathematical Result")
    if verdict == "PASS" and not verified_result:
        errors.append("PASS 报告缺少 `## Verified Mathematical Result`")
    assumptions = _section(markdown, "Assumption and Dependency Check")
    if not assumptions:
        errors.append("缺少 `## Assumption and Dependency Check`")
    issues = _section(markdown, "Decisive Mathematical Analysis")
    if not issues:
        errors.append("缺少 `## Decisive Mathematical Analysis`")
    reuse_boundary = _section(markdown, "Reuse Boundary")
    if not reuse_boundary:
        errors.append("缺少 `## Reuse Boundary`")
    validation = ValidationResult(errors, [])
    if not validation.ok:
        return None, validation
    return VerificationDecision(
        verdict, statement, issues, markdown, reuse_boundary, verified_result
    ), validation
