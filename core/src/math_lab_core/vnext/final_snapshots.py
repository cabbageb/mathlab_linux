from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
import json

from sqlalchemy import func, select

from .artifacts import ArtifactStore, sha256_file
from .db import (
    ArchitectureCommunicationRecord,
    ArchitectureDecisionLedgerRecord,
    ArchitectureReviewRecord,
    ArchitectureSnapshotRecord,
    Database,
    GlobalStateDeltaRecord,
    ResearchGraphEdgeRecord,
    ResearchNodeRecord,
    ResearchRunRecord,
    RuntimeEventRecord,
    utc_now,
)
from .repositories import RepositoryHub


class FinalSnapshotService:
    """从终态 DB 生成一次性公开快照，使页面不依赖可清理的 Agent runtime。"""

    def __init__(self, database: Database, artifacts: ArtifactStore, retention_days: int) -> None:
        self.database = database
        self.artifacts = artifacts
        self.retention_days = max(0, int(retention_days))

    def export_run(self, run_id: str) -> Path:
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise FileNotFoundError("Research Run 不存在")
            run_root = self.artifacts.resolve(run.run_root)
            final_root = run_root / "final"
            final_root.mkdir(parents=True, exist_ok=True)
            nodes = repo.list_nodes(run_id)
            edges = list(session.scalars(select(ResearchGraphEdgeRecord).where(
                ResearchGraphEdgeRecord.run_id == run_id
            ).order_by(ResearchGraphEdgeRecord.created_at)))
            public_events = repo.read_events("public_run", run_id)
            verified: list[dict[str, Any]] = []
            for claim in repo.list_verified_claims(run_id):
                summary_event = repo.latest_public_summary(claim.verified_claim_id)
                summary = claim.public_summary or (summary_event.message if summary_event else None)
                node = session.get(ResearchNodeRecord, claim.node_id)
                claim.public_summary = summary
                if node is not None and claim.title == "Verified mathematical result":
                    claim.title = node.title
                if claim.candidate_path:
                    candidate_path = self.artifacts.resolve(claim.candidate_path)
                    if not candidate_path.is_file():
                        raise FileNotFoundError(
                            f"Verified Claim Candidate 不存在：{claim.verified_claim_id}"
                        )
                    if sha256_file(candidate_path) != claim.sha256:
                        raise RuntimeError(
                            f"Verified Claim Candidate SHA-256 不一致：{claim.verified_claim_id}"
                        )
                    durable_candidate = final_root / "verified_claims" / f"{claim.verified_claim_id}.md"
                    if candidate_path != durable_candidate:
                        self.artifacts.write_bytes(
                            durable_candidate.relative_to(self.artifacts.data_root),
                            candidate_path.read_bytes(),
                        )
                    claim.candidate_path = self.artifacts.reference(durable_candidate)
                verified.append({
                    "verified_claim_id": claim.verified_claim_id,
                    "node_id": claim.node_id,
                    "title": claim.title,
                    "verdict": claim.verdict,
                    "statement": claim.statement,
                    "statement_sha256": claim.statement_sha256,
                    "proof_sha256": claim.proof_sha256,
                    "candidate_sha256": claim.candidate_sha256 or claim.sha256,
                    "verifier_report_sha256": claim.verifier_report_sha256,
                    "candidate_scope": claim.candidate_scope,
                    "controlling_proposition_id": claim.controlling_proposition_id,
                    "reuse_boundary": claim.reuse_boundary,
                    "node_version": claim.node_version,
                    "graph_version": claim.graph_version,
                    "verification_policy_version": claim.verification_policy_version,
                    "certificate_path": claim.certificate_path,
                    "public_summary": summary,
                    "created_at": claim.created_at.isoformat(),
                })
            usage = repo.run_usage_summary(run.owner_user_id, run_id)
            summary = {
                "schema_version": 1,
                "run_id": run.run_id,
                "conversation_id": run.conversation_id,
                "contract_version_id": run.contract_version_id,
                "status": run.status,
                "completion_kind": run.completion_kind,
                "graph_version": run.graph_version,
                "execution_policy": dict(run.execution_policy or {}),
                "expected_credit": float(run.expected_cost or 0),
                "actual_credit": float(run.actual_cost or 0),
                "settled_credit": float(run.settled_cost or 0),
                "usage": usage,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "terminal_at": (run.terminal_at or run.completed_at).isoformat() if (run.terminal_at or run.completed_at) else None,
                "final_paper_pdf_path": run.final_paper_pdf_path,
                "final_paper_tex_path": run.final_paper_tex_path,
            }
            graph = {
                "schema_version": 1,
                "run_id": run_id,
                "graph_version": run.graph_version,
                "nodes": [
                    {
                        "node_id": node.node_id,
                        "parent_node_id": node.parent_node_id,
                        "kind": node.kind,
                        "title": node.title,
                        "objective": node.objective,
                        "completion_criterion": node.completion_criterion,
                        "status": node.status,
                        "merged_into_node_id": node.merged_into_node_id,
                    }
                    for node in nodes
                ],
                "edges": [
                    {
                        "source_node_id": edge.source_node_id,
                        "target_node_id": edge.target_node_id,
                        "relation": edge.relation,
                        "condition": edge.condition,
                    }
                    for edge in edges
                ],
            }
            events = [
                {
                    "sequence": item.sequence,
                    "category": item.kind,
                    "title": str((item.payload or {}).get("title") or "研究进展"),
                    "summary": item.message,
                    "node_id": (item.payload or {}).get("node_id"),
                    "tone": str((item.payload or {}).get("tone") or "research"),
                    "created_at": item.created_at.isoformat(),
                }
                for item in public_events
            ]
            architecture_snapshot = session.get(ArchitectureSnapshotRecord, run_id)
            governance = {
                "schema_version": 1,
                "snapshot": dict(architecture_snapshot.payload or {}) if architecture_snapshot else {},
                "decision_ledger": [
                    {
                        "sequence": item.sequence,
                        "title": item.title,
                        "markdown": item.markdown,
                        "graph_version": item.graph_version,
                        "source_review_id": item.source_review_id,
                    }
                    for item in session.scalars(select(ArchitectureDecisionLedgerRecord).where(
                        ArchitectureDecisionLedgerRecord.run_id == run_id
                    ).order_by(ArchitectureDecisionLedgerRecord.sequence))
                ],
                "reviews": [
                    {
                        "review_id": item.review_id,
                        "sequence": item.sequence,
                        "trigger_reasons": list(item.trigger_reasons or []),
                        "graph_version_before": item.graph_version_before,
                        "graph_version_after": item.graph_version_after,
                        "input_snapshot_revision": item.input_snapshot_revision,
                        "input_node_preconditions": dict(item.input_node_preconditions or {}),
                        "application_status": item.application_status,
                        "actions": list(item.actions or []),
                    }
                    for item in session.scalars(select(ArchitectureReviewRecord).where(
                        ArchitectureReviewRecord.run_id == run_id
                    ).order_by(ArchitectureReviewRecord.sequence))
                ],
                "communications": [
                    {
                        "communication_id": item.communication_id,
                        "node_id": item.node_id,
                        "attention": item.attention,
                        "title": item.title,
                        "question": item.question,
                        "status": item.status,
                    }
                    for item in session.scalars(select(ArchitectureCommunicationRecord).where(
                        ArchitectureCommunicationRecord.run_id == run_id
                    ).order_by(ArchitectureCommunicationRecord.created_at))
                ],
                "delta_count": int(session.scalar(select(func.count()).select_from(
                    GlobalStateDeltaRecord
                ).where(GlobalStateDeltaRecord.run_id == run_id)) or 0),
            }

        references = self._collect_references(run_root)
        self.artifacts.write_json(final_root.relative_to(self.artifacts.data_root) / "run_summary.json", summary)
        self.artifacts.write_json(final_root.relative_to(self.artifacts.data_root) / "research_graph.json", graph)
        self.artifacts.write_json(final_root.relative_to(self.artifacts.data_root) / "verified_mathematics.json", verified)
        self.artifacts.write_json(final_root.relative_to(self.artifacts.data_root) / "streaming_events.json", events)
        self.artifacts.write_json(final_root.relative_to(self.artifacts.data_root) / "architecture_governance.json", governance)
        self.artifacts.write_json(final_root.relative_to(self.artifacts.data_root) / "references.json", references)

        with self.database.session() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise FileNotFoundError("Research Run 不存在")
            now = utc_now()
            run.final_snapshot_completed_at = now
            terminal = run.terminal_at or run.completed_at or now
            run.terminal_at = terminal
            run.runtime_expires_at = terminal + timedelta(days=self.retention_days)
        return final_root

    @staticmethod
    def _collect_references(run_root: Path) -> list[dict[str, Any]]:
        """只从正式文献角色冻结且完成阅读证据的目录导出引用。"""
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        paths: set[Path] = set()
        for pattern in (
            "runtime/nodes/*/literature/*/assignment-v*/papers/*/metadata.json",
            "final/scouts/*/literature/papers/*/metadata.json",
            "runtime/publication-replays/*/scouts/*/literature/papers/*/metadata.json",
        ):
            paths.update(run_root.glob(pattern))
        for path in sorted(paths):
            if not (path.parent / "reading_evidence.json").is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            arxiv_id = str(payload.get("arxiv_id") or payload.get("id") or "")
            version = str(payload.get("version") or "")
            key = (arxiv_id, version)
            if not arxiv_id or key in seen:
                continue
            seen.add(key)
            result.append({
                "arxiv_id": arxiv_id,
                "version": version,
                "title": payload.get("title"),
                "authors": payload.get("authors"),
                "url": payload.get("url") or payload.get("abs_url"),
                "source_sha256": payload.get("source_sha256"),
                "pdf_sha256": payload.get("pdf_sha256"),
                "evidence_level": payload.get("evidence_level") or "source_metadata",
            })
        return result
