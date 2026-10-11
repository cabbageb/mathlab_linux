from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
import json

from sqlalchemy import MetaData, Table, select, update
from sqlalchemy.engine import Connection


ARTIFACT_ANCHORS = ("users", "platform", "artifacts", "research_runs", "runtime")
SCALAR_PATH_COLUMNS = {
    "contract_generation_attempts": ("generation_attempt_id", "request_snapshot_path"),
    "contract_versions": ("contract_version_id", "markdown_path"),
    "file_assets": ("file_id", "artifact_path"),
    "research_runs": (
        "run_id",
        "run_root",
        "contract_snapshot_path",
        "source_snapshot_path",
        "handoff_manifest_path",
        "final_report_path",
        "final_paper_pdf_path",
        "final_paper_tex_path",
    ),
    "research_nodes": ("node_id", "final_stage_path"),
    "worker_reports": ("report_id", "markdown_path"),
    "architecture_reviews": ("review_id", "decision_markdown_path"),
    "architecture_snapshots": ("run_id", "snapshot_markdown_path"),
    "architecture_communications": ("communication_id", "source_markdown_path"),
    "verified_claims": (
        "verified_claim_id",
        "candidate_path",
        "verifier_report_path",
    ),
}
JSON_COLUMNS = {
    "contract_generation_attempts": ("generation_attempt_id", "source_manifest"),
    "contract_versions": ("contract_version_id", "source_manifest"),
    "working_drafts": ("conversation_id", "source_manifest"),
    "research_nodes": ("node_id", "details"),
    "promotion_proposals": ("proposal_id", "recommended_actions"),
    "architecture_reviews": ("review_id", "actions"),
    "architecture_snapshots": ("run_id", "payload"),
    "global_state_deltas": ("delta_id", "old_summary", "new_summary", "evidence_refs"),
    "architecture_communications": (
        "communication_id", "affected_node_ids", "related_evidence_ids"
    ),
    "runtime_events": ("event_id", "payload"),
}
PATH_KEYS = {
    "artifact_path",
    "candidate_path",
    "contract_snapshot_path",
    "decision_markdown_path",
    "final_report_path",
    "final_paper_pdf_path",
    "final_paper_tex_path",
    "final_stage_path",
    "handoff_manifest_path",
    "markdown_path",
    "path",
    "request_snapshot_path",
    "run_root",
    "snapshot_path",
    "snapshot_markdown_path",
    "source_markdown_path",
    "source_snapshot_path",
    "verifier_report_path",
}


@dataclass
class ArtifactMigrationReport:
    """记录相对路径迁移的变更数和无法安全识别的路径。"""

    database_values_changed: int = 0
    json_files_changed: int = 0
    unknown_paths: list[str] = field(default_factory=list)

    def require_clean(self) -> None:
        if self.unknown_paths:
            sample = "\n".join(f"- {item}" for item in self.unknown_paths[:20])
            raise RuntimeError(f"发现无法归入 MATHLAB_DATA_ROOT 的绝对路径：\n{sample}")


def portable_reference(value: str) -> str:
    """将旧绝对路径按稳定目录锚点改写为 POSIX 相对 Artifact 引用。"""
    text = value.strip()
    if not text:
        return text
    path = Path(text)
    if not path.is_absolute():
        pure = PurePosixPath(text.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(text)
        return pure.as_posix()
    parts = path.parts
    candidates = [index for index, part in enumerate(parts) if part in ARTIFACT_ANCHORS]
    if not candidates:
        raise ValueError(text)
    return PurePosixPath(*parts[candidates[-1] :]).as_posix()


def rewrite_path_tree(value: Any, report: ArtifactMigrationReport, *, key: str = "") -> tuple[Any, bool]:
    """递归改写 JSON 中明确承载 Artifact 路径的字段。"""
    if isinstance(value, dict):
        changed = False
        rewritten: dict[str, Any] = {}
        for child_key, child_value in value.items():
            next_value, child_changed = rewrite_path_tree(
                child_value, report, key=str(child_key)
            )
            rewritten[str(child_key)] = next_value
            changed = changed or child_changed
        return rewritten, changed
    if isinstance(value, list):
        changed = False
        rewritten_list: list[Any] = []
        for child in value:
            next_value, child_changed = rewrite_path_tree(child, report, key=key)
            rewritten_list.append(next_value)
            changed = changed or child_changed
        return rewritten_list, changed
    if isinstance(value, str) and key in PATH_KEYS and value.strip():
        try:
            rewritten = portable_reference(value)
        except ValueError:
            if Path(value).is_absolute():
                report.unknown_paths.append(value)
            return value, False
        return rewritten, rewritten != value
    return value, False


def migrate_database_references(
    connection: Connection,
    *,
    apply: bool,
) -> ArtifactMigrationReport:
    """扫描现有数据库路径字段；clean 后可原地改写，dry-run 不写数据库。"""
    report = ArtifactMigrationReport()
    metadata = MetaData()
    metadata.reflect(bind=connection)
    pending: list[tuple[Table, Any, str, Any]] = []

    for table_name, columns in SCALAR_PATH_COLUMNS.items():
        table = metadata.tables.get(table_name)
        if table is None:
            continue
        primary_key, *path_columns = columns
        path_columns = [name for name in path_columns if name in table.c]
        rows = connection.execute(select(table.c[primary_key], *(table.c[name] for name in path_columns)))
        for row in rows.mappings():
            for column in path_columns:
                value = row[column]
                if not isinstance(value, str) or not value.strip():
                    continue
                try:
                    rewritten = portable_reference(value)
                except ValueError:
                    if Path(value).is_absolute():
                        report.unknown_paths.append(f"{table_name}.{column}:{row[primary_key]}={value}")
                    continue
                if rewritten != value:
                    pending.append((table, table.c[primary_key], row[primary_key], {column: rewritten}))

    for table_name, columns in JSON_COLUMNS.items():
        table = metadata.tables.get(table_name)
        if table is None:
            continue
        primary_key, *json_columns = columns
        json_columns = [name for name in json_columns if name in table.c]
        rows = connection.execute(select(table.c[primary_key], *(table.c[name] for name in json_columns)))
        for row in rows.mappings():
            for column in json_columns:
                value = row[column]
                if value is None:
                    continue
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        continue
                rewritten, changed = rewrite_path_tree(value, report)
                if changed:
                    pending.append((table, table.c[primary_key], row[primary_key], {column: rewritten}))

    report.require_clean()
    report.database_values_changed = len(pending)
    if apply:
        for table, primary_key, identity, values in pending:
            connection.execute(update(table).where(primary_key == identity).values(**values))
    return report


def migrate_json_manifests(data_root: Path, *, apply: bool) -> ArtifactMigrationReport:
    """迁移数据根内 handoff、source manifest 与请求快照的嵌套路径。"""
    root = data_root.expanduser().resolve()
    report = ArtifactMigrationReport()
    pending: list[tuple[Path, dict[str, Any] | list[Any]]] = []
    manifest_paths = [
        path
        for subtree in (root / "artifacts", root / "research_runs")
        if subtree.is_dir()
        for path in subtree.rglob("*.json")
    ]
    for path in sorted(manifest_paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        rewritten, changed = rewrite_path_tree(payload, report)
        if changed:
            pending.append((path, rewritten))
    report.require_clean()
    report.json_files_changed = len(pending)
    if apply:
        for path, payload in pending:
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
    return report
