from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import re
import shutil

from sqlalchemy import select

from math_lab_core.vnext.artifacts import ArtifactStore, safe_filename, sha256_file
from math_lab_core.vnext.db import (
    ArchitectureReviewRecord,
    Database,
    ResearchFindingRecord,
    ResearchNodeRecord,
    ResearchRunRecord,
    VerifiedClaimRecord,
    WorkerAssignmentRecord,
    WorkerReportRecord,
)
from math_lab_core.vnext.repositories import RepositoryHub


_MAX_RAW_ARCHIVE_FILES = 256
_MAX_RAW_TEXT_CHARS = 500_000
_HIGH_VALUE_FINDING_TYPES = {
    "candidate_answer",
    "possible_claim",
    "possible_lemma",
    "near_counterexample",
    "mechanism",
    "proof_mechanism",
    "structural_obstruction",
    "proof_route",
    "parameter_regime",
    "unexpected_reduction",
    "missing_bridge",
    "new_question",
}


@dataclass(frozen=True)
class PaperSourceCorpus:
    """Curated mathematical source package for the final manuscript roles.

    The Writer receives the curated entry points plus a bounded mathematical archive.
    The Auditor additionally receives internal verification-boundary artifacts.  Neither
    role receives run status, budget, architecture-review chronology, token usage, or
    other project-management material through this object.
    """

    root: Path
    manifest_path: Path
    writer_context_paths: tuple[Path, ...]
    auditor_context_paths: tuple[Path, ...]
    verified_result_count: int
    genuine_source_count: int


class PaperCorpusError(RuntimeError):
    pass


def build_paper_source_corpus(
    database: Database,
    artifacts: ArtifactStore,
    run_id: str,
    *,
    output_root: Path | None = None,
) -> PaperSourceCorpus:
    """Build a two-layer Paper Source Corpus for a completed Research Run.

    Layer 1 is curated and small: mathematical problem, theorem-eligible verified
    results, discussion-only findings, open mathematical gaps, and genuine sources.
    Layer 2 is a bounded archive that the Writer may inspect on demand: latest Worker
    reports, Finding source reports, candidates, verifier assessments, and Final Local
    Stages.  Architecture reviews, runtime events, budgets, completion kinds, and usage
    are deliberately excluded.
    """

    if output_root is None:
        with database.session() as session:
            record = session.get(ResearchRunRecord, run_id)
            if record is None:
                raise PaperCorpusError(f"Research Run 不存在：{run_id}")
            root = artifacts.resolve(record.run_root) / "runtime" / "publication" / "paper-source-corpus"
    else:
        root = output_root
    staging_root = root.with_name(".paper-source-corpus.tmp")
    if staging_root.exists():
        shutil.rmtree(staging_root)
    writer_root = staging_root / "writer"
    auditor_root = staging_root / "auditor"
    writer_root.mkdir(parents=True, exist_ok=True)
    auditor_root.mkdir(parents=True, exist_ok=True)

    writer_paths: list[Path] = []
    auditor_only_paths: list[Path] = []
    formal_entries: list[dict[str, Any]] = []
    stage_entries: list[dict[str, Any]] = []
    raw_entries: list[dict[str, Any]] = []
    source_entries: list[dict[str, Any]] = []

    with database.session() as session:
        repo = RepositoryHub(session, artifacts)
        run = session.get(ResearchRunRecord, run_id)
        if run is None:
            raise PaperCorpusError(f"Research Run 不存在：{run_id}")
        run_root = artifacts.resolve(run.run_root)

        contract_path = artifacts.resolve(run.contract_snapshot_path)
        contract_text = _read_text(contract_path)
        if not contract_text.strip():
            raise PaperCorpusError("Contract snapshot 不存在或无法读取")
        mathematical_contract = _extract_contract_section(contract_text, "A")
        problem_path = writer_root / "01-mathematical-problem-and-scope.md"
        problem_path.write_text(
            "\n".join(
                [
                    "# Mathematical Problem and Scope",
                    "",
                    "The following material describes the mathematical problem, assumptions,",
                    "full-answer scope, and user-authorized research boundaries.  It is source",
                    "material for exposition, not wording that must appear in the manuscript.",
                    "",
                    "## Mathematical problem, objects, assumptions, and answer scope",
                    "",
                    mathematical_contract.strip() or contract_text.strip(),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        writer_paths.append(problem_path)

        claims = repo.list_verified_claims(run_id)
        claim_nodes = {claim.node_id for claim in claims if claim.verdict.upper() == "PASS"}
        for index, claim in enumerate(claims, start=1):
            if claim.verdict.upper() != "PASS":
                continue
            if not claim.statement.strip() or not claim.candidate_path:
                raise PaperCorpusError(
                    f"Verified Claim 缺少正式 statement/Candidate：{claim.verified_claim_id}"
                )
            candidate_path = artifacts.resolve(claim.candidate_path)
            if not candidate_path.is_file():
                raise PaperCorpusError(
                    f"Verified Claim Candidate 不存在：{claim.verified_claim_id}"
                )
            if claim.sha256 and sha256_file(candidate_path) != claim.sha256:
                raise PaperCorpusError(
                    f"Verified Claim Candidate SHA-256 不一致：{claim.verified_claim_id}"
                )
            candidate_text = _read_text(candidate_path)
            if not candidate_text.strip():
                raise PaperCorpusError(
                    f"Verified Claim Candidate 无法读取：{claim.verified_claim_id}"
                )
            formal_path = writer_root / f"10-formal-result-{index:03d}.md"
            formal_path.write_text(
                "\n".join(
                    [
                        f"# Formal Mathematical Result {index}",
                        "",
                        "Editorial admissibility: FORMAL_RESULT.",
                        "",
                        "This result may be presented as a theorem, proposition, lemma,",
                        "corollary, classification statement, or verified counterexample.",
                        "The manuscript must not mention this editorial label, verification",
                        "workflow, database identifiers, or provenance machinery.",
                        "",
                        "## Exact mathematical statement",
                        "",
                        claim.statement.strip(),
                        "",
                        "## Proof material",
                        "",
                        candidate_text.strip(),
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            writer_paths.append(formal_path)
            formal_entries.append(
                {
                    "label": f"Formal Result {index}",
                    "file": formal_path.name,
                    "node_id": claim.node_id,
                }
            )

            if not claim.verifier_report_path:
                raise PaperCorpusError(
                    f"Verified Claim Verifier report 未登记：{claim.verified_claim_id}"
                )
            verifier_path = artifacts.resolve(claim.verifier_report_path)
            if not verifier_path.is_file():
                raise PaperCorpusError(
                    f"Verified Claim Verifier report 不存在：{claim.verified_claim_id}"
                )
            if (
                claim.verifier_report_sha256
                and sha256_file(verifier_path) != claim.verifier_report_sha256
            ):
                raise PaperCorpusError(
                    f"Verified Claim Verifier report SHA-256 不一致：{claim.verified_claim_id}"
                )
            verifier_text = _read_text(verifier_path)
            boundary_path = auditor_root / f"80-editorial-verification-boundary-{index:03d}.md"
            boundary_path.write_text(
                "\n".join(
                    [
                        f"# Editorial Verification Boundary {index}",
                        "",
                        "This file is for the independent manuscript auditor.  It is not",
                        "manuscript content and must never be quoted or paraphrased as part of",
                        "the paper's research narrative.",
                        "",
                        f"Internal verified-claim identifier: {claim.verified_claim_id}",
                        "",
                        "## Independent assessment",
                        "",
                        verifier_text.strip() or "[Verifier report unavailable.]",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            auditor_only_paths.append(boundary_path)

        nodes = repo.list_nodes(run_id)
        open_gap_blocks: list[str] = []
        pending_feedback_blocks: list[str] = []
        open_gap_index = 0
        for index, node in enumerate(nodes, start=1):
            details = dict(node.details or {})
            stage_text = ""
            if node.final_stage_path:
                stage_path = artifacts.resolve(node.final_stage_path)
                if stage_path.is_file() and "degraded" not in stage_path.name.lower():
                    if node.final_stage_sha256 and sha256_file(stage_path) != node.final_stage_sha256:
                        raise PaperCorpusError(
                            f"Final Local Stage SHA-256 不一致：{node.node_id}"
                        )
                    stage_text = _read_text(stage_path)
            latest_review = ""
            latest_review_ref = str(details.get("latest_supervisor_review_path") or "")
            if latest_review_ref:
                latest_review_path = artifacts.resolve(latest_review_ref)
                if latest_review_path.is_file():
                    expected = str(details.get("latest_supervisor_review_sha256") or "")
                    if expected and sha256_file(latest_review_path) != expected:
                        raise PaperCorpusError(
                            f"Supervisor Review SHA-256 不一致：{node.node_id}"
                        )
                    latest_review = _read_text(latest_review_path)
            latest_diagnosis = str(details.get("latest_supervisor_diagnosis") or "")
            material = stage_text.strip() or latest_review.strip() or latest_diagnosis.strip()
            for feedback in list(details.get("verification_feedback_inbox") or []):
                item = dict(feedback)
                if str(item.get("status") or "pending") != "pending":
                    continue
                body = str(item.get("markdown") or item.get("error") or "").strip()
                if not body:
                    continue
                pending_feedback_blocks.extend([
                    f"## Verification feedback for {node.title}",
                    "",
                    "Editorial admissibility: DISCUSSION_ONLY_OR_OPEN_GAP.",
                    "",
                    body,
                    "",
                ])
            if not material:
                continue
            editorial_class = "SUPPORTING_MATERIAL" if node.node_id in claim_nodes else "DISCUSSION_ONLY_OR_OPEN_GAP"
            stage_path = writer_root / f"20-mathematical-stage-{index:03d}.md"
            stage_path.write_text(
                "\n".join(
                    [
                        f"# Mathematical Stage Material {index}",
                        "",
                        f"Editorial admissibility: {editorial_class}.",
                        "",
                        "Use this file only for mathematical definitions, reductions, proof",
                        "ideas, conditional statements, obstructions, or unresolved analytic",
                        "interfaces.  Do not reproduce internal role names, statuses, or chronology.",
                        "",
                        "## Mathematical objective",
                        "",
                        node.objective.strip(),
                        "",
                        "## Completion criterion originally attached to the problem",
                        "",
                        node.completion_criterion.strip(),
                        "",
                        "## Available mathematical material",
                        "",
                        material,
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            writer_paths.append(stage_path)
            stage_entries.append(
                {
                    "label": f"Mathematical Stage {index}",
                    "file": stage_path.name,
                    "editorial_class": editorial_class,
                }
            )
            if node.node_id not in claim_nodes:
                open_gap_index += 1
                open_gap_blocks.extend(
                    [
                        f"## Open mathematical interface {open_gap_index}",
                        "",
                        node.objective.strip(),
                        "",
                        "### Current mathematical material",
                        "",
                        material,
                        "",
                    ]
                )

        open_gap_path = writer_root / "30-open-mathematical-gaps.md"
        open_gap_path.write_text(
            "\n".join(
                [
                    "# Open Mathematical Gaps",
                    "",
                    "These are mathematical interfaces that remain unresolved or only",
                    "conditionally established.  Express them in the manuscript as open",
                    "problems, remaining analytic issues, conditional hypotheses, or",
                    "limitations.  Never explain them through budget, run status, or Agent",
                    "chronology.",
                    "",
                    *(open_gap_blocks or ["No separate open mathematical gap was extracted.", ""]),
                ]
            ),
            encoding="utf-8",
        )
        writer_paths.append(open_gap_path)

        feedback_path = writer_root / "35-pending-verification-feedback.md"
        feedback_path.write_text(
            "\n".join([
                "# Pending Verification Feedback",
                "",
                "These materials did not become verified mathematics.  Use them only to",
                "explain rejected candidates, unresolved proof gaps, or necessary revisions.",
                "Never present them as theorem-level conclusions.",
                "",
                *(pending_feedback_blocks or ["No pending verification feedback was recorded.", ""]),
            ]),
            encoding="utf-8",
        )
        writer_paths.append(feedback_path)

        findings = list(
            session.scalars(
                select(ResearchFindingRecord)
                .join(ResearchNodeRecord)
                .where(ResearchNodeRecord.run_id == run_id)
                .order_by(ResearchFindingRecord.created_at)
            )
        )
        selected_findings = [item for item in findings if _finding_is_paper_relevant(item)]
        discussion_path = writer_root / "40-discussion-only-mathematical-material.md"
        discussion_lines = [
            "# Discussion-Only Mathematical Material",
            "",
            "Every item in this file is DISCUSSION_ONLY unless it is independently",
            "restated in a FORMAL_RESULT file.  It may support a conjecture, heuristic",
            "paragraph, mathematical question, obstruction, sharpness discussion, or",
            "future direction.  It must not be presented as a proved theorem.",
            "",
        ]
        for index, item in enumerate(selected_findings, start=1):
            discussion_lines.extend(
                [
                    f"## Discussion item {index}: {item.title}",
                    "",
                    f"Mathematical type: {item.finding_type}",
                    "",
                    item.markdown.strip(),
                    "",
                ]
            )
        if not selected_findings:
            discussion_lines.extend(["No separate discussion-only Finding was selected.", ""])
        discussion_path.write_text("\n".join(discussion_lines), encoding="utf-8")
        writer_paths.append(discussion_path)

        latest_review = session.scalar(
            select(ArchitectureReviewRecord)
            .where(
                ArchitectureReviewRecord.run_id == run_id,
                ArchitectureReviewRecord.application_status == "applied",
            )
            .order_by(ArchitectureReviewRecord.sequence.desc())
            .limit(1)
        )
        diagnosis = ""
        if latest_review is not None:
            review_text = _read_text(
                artifacts.resolve(latest_review.decision_markdown_path)
            )
            diagnosis = _markdown_section(review_text, "Global Diagnosis")
        reviewer_path = writer_root / "45-global-mathematical-assessment.md"
        reviewer_path.write_text(
            "\n".join([
                "# Global Mathematical Assessment",
                "",
                "Editorial admissibility: DISCUSSION_ONLY_OR_OPEN_GAP unless the same",
                "statement appears in a FORMAL_RESULT file.",
                "",
                diagnosis or "No separate global mathematical assessment was recorded.",
                "",
            ]),
            encoding="utf-8",
        )
        writer_paths.append(reviewer_path)

        source_manifest_path = artifacts.resolve(run.source_snapshot_path).parent / "source_manifest.json"
        source_manifest = _read_json(source_manifest_path)
        source_index_lines = [
            "# Genuine Source Materials",
            "",
            "Only the source files listed here may be cited as external literature or",
            "bibliographic sources.  Internal mathematical artifacts are not references.",
            "If this list is empty, omit the References section entirely.",
            "",
        ]
        if isinstance(source_manifest, list):
            for index, item in enumerate(source_manifest, start=1):
                if not isinstance(item, dict) or item.get("availability") != "frozen":
                    continue
                snapshot_ref = str(item.get("snapshot_path") or "")
                snapshot = artifacts.resolve(snapshot_ref) if snapshot_ref else Path()
                if not snapshot.is_file():
                    continue
                target = writer_root / f"50-source-{index:03d}-{safe_filename(str(item.get('original_name') or snapshot.name))}"
                shutil.copy2(snapshot, target)
                writer_paths.append(target)
                source_entries.append(
                    {
                        "file": target.name,
                        "original_name": str(item.get("original_name") or snapshot.name),
                        "sha256": str(item.get("snapshot_sha256") or item.get("sha256") or ""),
                    }
                )
                source_index_lines.extend(
                    [
                        f"## Source {len(source_entries)}",
                        "",
                        f"File: `{target.name}`",
                        f"Original name: `{item.get('original_name') or snapshot.name}`",
                        f"SHA-256: `{item.get('snapshot_sha256') or item.get('sha256') or ''}`",
                        "",
                    ]
                )
        runtime_sources = _collect_runtime_literature_sources(run_root)
        for item in runtime_sources:
            source_number = len(source_entries) + 1
            arxiv_id = str(item["metadata"].get("arxiv_id") or "unknown")
            source_dir = writer_root / f"50-runtime-source-{source_number:03d}-{safe_filename(arxiv_id)}"
            shutil.copytree(item["paper_root"], source_dir)
            card = writer_root / f"50-runtime-source-{source_number:03d}.md"
            metadata = dict(item["metadata"])
            authors = ", ".join(str(author) for author in list(metadata.get("authors") or []))
            card.write_text(
                "\n".join(
                    [
                        f"# Genuine Runtime Literature Source {source_number}",
                        "",
                        "Evidence level: FULL_TEXT_VERIFIED",
                        f"Title: {metadata.get('title') or '[title unavailable]'}",
                        f"Authors: {authors or '[authors unavailable]'}",
                        f"Year: {metadata.get('year') or '[year unavailable]'}",
                        f"arXiv: {arxiv_id}",
                        f"Exact version: {metadata.get('exact_version') or arxiv_id}",
                        f"Link: {metadata.get('canonical_url') or ''}",
                        f"PDF SHA-256: {metadata.get('pdf_sha256') or ''}",
                        f"Materialized files: `{source_dir.name}/`",
                        "",
                        "The exact source/PDF and Controller-generated reading evidence are",
                        "available in the materialized directory. Use any theorem-level claim",
                        "only within the locations actually recorded in `reading_evidence.json`.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            writer_paths.extend([card, source_dir])
            source_entries.append(
                {
                    "file": card.name,
                    "original_name": str(metadata.get("title") or arxiv_id),
                    "sha256": str(metadata.get("pdf_sha256") or metadata.get("source_sha256") or ""),
                    "arxiv_id": arxiv_id,
                    "evidence_level": "FULL_TEXT_VERIFIED",
                }
            )
            source_index_lines.extend(
                [
                    f"## Source {source_number}",
                    "",
                    f"File: `{card.name}`",
                    f"Title: {metadata.get('title') or '[title unavailable]' }",
                    f"Authors: {authors or '[authors unavailable]' }",
                    f"arXiv: {arxiv_id}",
                    f"Link: {metadata.get('canonical_url') or ''}",
                    "Evidence level: FULL_TEXT_VERIFIED",
                    "",
                ]
            )
        if not source_entries:
            source_index_lines.extend(["No genuine external source material is available.", ""])
        source_index_path = writer_root / "50-genuine-source-index.md"
        source_index_path.write_text("\n".join(source_index_lines), encoding="utf-8")
        writer_paths.append(source_index_path)

        accepted_verifier_paths = {
            str(artifacts.resolve(claim.verifier_report_path))
            for claim in claims
            if claim.verdict.upper() == "PASS"
        }
        raw_paths = _collect_bounded_raw_mathematical_archive(
            session, artifacts, run_id, findings, accepted_verifier_paths=accepted_verifier_paths
        )
        for index, source in enumerate(raw_paths, start=1):
            target = writer_root / f"70-raw-mathematical-archive-{index:03d}-{safe_filename(source.name)}"
            _copy_bounded(source, target)
            writer_paths.append(target)
            raw_entries.append({"file": target.name, "source_name": source.name})

    manifest_path = writer_root / "00-paper-source-manifest.md"
    manifest_path.write_text(
        _render_manifest(
            formal_entries=formal_entries,
            stage_entries=stage_entries,
            source_entries=source_entries,
            raw_entries=raw_entries,
        ),
        encoding="utf-8",
    )

    if root.exists():
        shutil.rmtree(root)
    staging_root.replace(root)

    # Publication roles receive directory trees rather than prompt-expanded file
    # contents.  The Writer starts from the curated writer/ manifest and opens raw
    # mathematical artifacts only as needed.  The Auditor additionally receives the
    # auditor/ verification-boundary directory.
    return PaperSourceCorpus(
        root=root,
        manifest_path=root / "writer" / manifest_path.name,
        writer_context_paths=(root / "writer",),
        auditor_context_paths=(root / "writer", root / "auditor"),
        verified_result_count=len(formal_entries),
        genuine_source_count=len(source_entries),
    )


def _collect_runtime_literature_sources(run_root: Path) -> list[dict[str, Any]]:
    """收集真实下载且已形成阅读证据的研究期文献，并按 exact source 去重。"""
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    nodes_root = run_root / "runtime" / "nodes"
    if not nodes_root.is_dir():
        return []
    for metadata_path in sorted(nodes_root.glob("*/literature/*/assignment-v*/papers/*/metadata.json")):
        paper_root = metadata_path.parent
        reading_path = paper_root / "reading_evidence.json"
        if not reading_path.is_file():
            continue
        metadata = _read_json(metadata_path)
        if not isinstance(metadata, dict) or not (
            metadata.get("pdf_sha256") or metadata.get("source_sha256")
        ):
            continue
        arxiv_id = str(metadata.get("arxiv_id") or "")
        if not arxiv_id:
            continue
        key = (arxiv_id, str(metadata.get("pdf_sha256") or ""))
        selected.setdefault(key, {"metadata": metadata, "paper_root": paper_root})
    return list(selected.values())


def _render_manifest(
    *,
    formal_entries: list[dict[str, Any]],
    stage_entries: list[dict[str, Any]],
    source_entries: list[dict[str, Any]],
    raw_entries: list[dict[str, Any]],
) -> str:
    lines = [
        "# Paper Source Manifest",
        "",
        "This is editorial metadata for a mathematical author.  It is not part of the",
        "manuscript and must not be quoted, summarized, or exposed to the reader.",
        "",
        "## Non-negotiable manuscript rule",
        "",
        "Write a normal mathematical paper.  Organize it by definitions, theorems,",
        "lemmas, proofs, examples, counterexamples, and discussion—not by Research",
        "Nodes, Agent chronology, verification workflow, run status, budget, or",
        "database provenance.",
        "",
        "## Editorial classes",
        "",
        "- `FORMAL_RESULT`: may be stated as a theorem-level mathematical result.",
        "- `SUPPORTING_MATERIAL`: may be used to improve exposition or proofs, but only",
        "  within the boundary of the corresponding formal result.",
        "- `DISCUSSION_ONLY`: may appear only as conjecture, heuristic, mathematical",
        "  obstruction, negative insight, sharpness discussion, or open question.",
        "- `OPEN_GAP`: unresolved mathematics; state it honestly and mathematically.",
        "- `GENUINE_SOURCE`: the only class eligible for bibliography citations.",
        "- `RAW_ARCHIVE`: optional source material to inspect on demand; never treat its",
        "  internal status language as manuscript content.",
        "",
        "## Required entry points",
        "",
        "1. `01-mathematical-problem-and-scope.md`",
        "2. every `10-formal-result-*.md` file",
        "3. `30-open-mathematical-gaps.md`",
        "4. `35-pending-verification-feedback.md`",
        "5. `40-discussion-only-mathematical-material.md`",
        "6. `45-global-mathematical-assessment.md`",
        "7. `50-genuine-source-index.md`",
        "",
        "## Formal results",
        "",
    ]
    if formal_entries:
        lines.extend(f"- `{item['file']}`" for item in formal_entries)
    else:
        lines.append("- None.  Do not invent a theorem merely to make the paper look complete.")
    lines.extend(["", "## Supporting and open-gap material", ""])
    if stage_entries:
        lines.extend(
            f"- `{item['file']}` — {item['editorial_class']}" for item in stage_entries
        )
    else:
        lines.append("- None.")
    lines.extend(["", "## Genuine external sources", ""])
    if source_entries:
        lines.extend(f"- `{item['file']}`" for item in source_entries)
    else:
        lines.append("- None.  Omit the References section.")
    lines.extend(["", "## Optional raw mathematical archive", ""])
    if raw_entries:
        lines.extend(f"- `{item['file']}`" for item in raw_entries)
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Prohibited manuscript vocabulary",
            "",
            "The paper must not mention MathLab, Research Run, Research Contract,",
            "Research Node, Worker, Supervisor, Architecture Reviewer, Fresh Verifier,",
            "Verified Claim, Evidence Manifest, Promotion, claim/node/run identifiers,",
            "budget exhaustion, model calls, threads, tokens, or the chronology of the",
            "research system.  Those concepts are editorial machinery, not mathematics.",
            "",
        ]
    )
    return "\n".join(lines)


def _collect_bounded_raw_mathematical_archive(
    session: Any,
    artifacts: ArtifactStore,
    run_id: str,
    findings: list[ResearchFindingRecord],
    *,
    accepted_verifier_paths: set[str],
) -> list[Path]:
    candidates: list[Path] = []
    run = session.get(ResearchRunRecord, run_id)
    if run is None:
        raise PaperCorpusError(f"Research Run 不存在：{run_id}")
    run_root = artifacts.resolve(run.run_root)

    reports = list(
        session.execute(
            select(WorkerReportRecord, WorkerAssignmentRecord.worker_id)
            .join(WorkerAssignmentRecord, WorkerReportRecord.assignment_id == WorkerAssignmentRecord.assignment_id)
            .join(ResearchNodeRecord, WorkerAssignmentRecord.node_id == ResearchNodeRecord.node_id)
            .where(ResearchNodeRecord.run_id == run_id)
            .order_by(WorkerReportRecord.created_at)
        )
    )
    latest_by_worker: dict[str, WorkerReportRecord] = {}
    by_report_id: dict[str, WorkerReportRecord] = {}
    for report, worker_id in reports:
        latest_by_worker[str(worker_id)] = report
        by_report_id[report.report_id] = report
    for report in latest_by_worker.values():
        path = artifacts.resolve(report.markdown_path)
        if path.is_file():
            candidates.append(path)
    for finding in findings:
        if finding.source_report_id and finding.source_report_id in by_report_id:
            path = artifacts.resolve(by_report_id[finding.source_report_id].markdown_path)
            if path.is_file():
                candidates.append(path)

    for pattern in (
        "runtime/nodes/*/candidates/*.md",
        "runtime/nodes/*/verification/*.md",
        "runtime/nodes/*/final-local-stage*.md",
    ):
        for path in run_root.glob(pattern):
            if not path.is_file():
                continue
            if "degraded" in path.name.lower():
                continue
            if str(path.resolve()) in accepted_verifier_paths:
                # Accepted verifier reports are audit-only; the Writer already has the
                # exact formal statement and proof artifact in FORMAL_RESULT files.
                continue
            candidates.append(path)

    unique = _unique_paths(candidates)
    return unique[:_MAX_RAW_ARCHIVE_FILES]


def _finding_is_paper_relevant(item: ResearchFindingRecord) -> bool:
    return (
        item.status in {"nominated", "promoted"}
        or item.significance not in {"", "normal", None}
        or item.finding_type in _HIGH_VALUE_FINDING_TYPES
    )


def _markdown_section(markdown: str, heading: str) -> str:
    """读取一个二级 Markdown 章节，供出版语料裁剪全局数学判断。"""
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", markdown, flags=re.MULTILINE))
    expected = heading.strip().casefold()
    for index, match in enumerate(matches):
        if match.group(1).strip().casefold() != expected:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        return markdown[start:end].strip()
    return ""


def _extract_contract_section(markdown: str, letter: str) -> str:
    match = re.search(
        rf"(?ms)^##\s+{re.escape(letter)}\.\s*[^\n]*\n(?P<body>.*?)(?=^##\s+[A-F]\.\s|\Z)",
        markdown,
    )
    return match.group("body").strip() if match else ""


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_json(path: Path) -> object:
    text = _read_text(path)
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def _copy_bounded(source: Path, target: Path) -> None:
    try:
        text = source.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        shutil.copy2(source, target)
        return
    if len(text) > _MAX_RAW_TEXT_CHARS:
        text = text[:_MAX_RAW_TEXT_CHARS] + (
            f"\n\n[raw mathematical archive truncated after {_MAX_RAW_TEXT_CHARS} characters]\n"
        )
    target.write_text(text, encoding="utf-8")


def _unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(path)
    return result
