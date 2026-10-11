from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None) -> str | None:
    """把 SQLite 读出的 naive UTC 与 aware 时间统一序列化为带 Z 的 ISO。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Base(DeclarativeBase):
    pass


class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_users_storage_key"),
    )

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    storage_key: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, nullable=True, index=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    display_name: Mapped[str] = mapped_column(String(160))
    institution: Mapped[str | None] = mapped_column(String(240), nullable=True)
    department: Mapped[str | None] = mapped_column(String(240), nullable=True)
    position: Mapped[str | None] = mapped_column(String(160), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="user", index=True)
    is_developer: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    default_model_strategy: Mapped[str] = mapped_column(String(64), default="balanced")
    default_budget_preset: Mapped[str] = mapped_column(String(64), default="standard")
    website_language: Mapped[str] = mapped_column(String(16), default="zh")
    default_contract_language: Mapped[str] = mapped_column(String(16), default="zh")
    default_research_language: Mapped[str] = mapped_column(String(16), default="zh")
    contract_chat_panel_width: Mapped[float] = mapped_column(Float, default=0.34)
    credit_balance: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    monthly_cost_limit: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    storage_quota_bytes: Mapped[int | None] = mapped_column(BigInteger, default=536_870_912, nullable=True)
    max_concurrent_research_runs: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    projects: Mapped[list[ProjectRecord]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    conversations: Mapped[list[ContractConversationRecord]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class AllowedEmailDomainRecord(Base):
    __tablename__ = "allowed_email_domains"

    domain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    domain: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    source: Mapped[str] = mapped_column(String(32), default="admin")
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class UserStorageSequenceRecord(Base):
    """跨 SQLite/PostgreSQL 分配稳定、不可变的 user_000001 存储键。"""

    __tablename__ = "user_storage_sequence"

    sequence_name: Mapped[str] = mapped_column(String(32), primary_key=True, default="users")
    next_value: Mapped[int] = mapped_column(BigInteger, default=1)


class EmailDomainGroupRuleRecord(Base):
    """邮箱域名只决定自动 Group 与 welcome Credit，不再充当注册白名单。"""

    __tablename__ = "email_domain_group_rules"
    __table_args__ = (
        UniqueConstraint("domain", name="uq_email_domain_group_rules_domain"),
    )

    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    domain: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.group_id", ondelete="RESTRICT"), index=True)
    welcome_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    grant_key: Mapped[str] = mapped_column(String(96))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class EmailOtpRecord(Base):
    __tablename__ = "email_otps"

    otp_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    purpose: Mapped[str] = mapped_column(String(32), default="login")
    code_hash: Mapped[str] = mapped_column(String(128))
    salt: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending_delivery", index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_error_class: Mapped[str | None] = mapped_column(String(160), nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class EmailOtpSendReservationRecord(Base):
    """Serialize OTP delivery reservations for one normalized email address."""

    __tablename__ = "email_otp_send_reservations"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    otp_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class AuthSessionRecord(Base):
    __tablename__ = "auth_sessions"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GroupRecord(Base):
    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("system_key", name="uq_groups_system_key"),
    )

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    system_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    default_monthly_cost_limit: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    default_max_concurrent_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_model_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_budget_preset: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class GroupMembershipRecord(Base):
    __tablename__ = "group_memberships"
    __table_args__ = (UniqueConstraint("group_id", "user_id"),)

    membership_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("groups.group_id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CreditLedgerRecord(Base):
    __tablename__ = "credit_ledger"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_credit_ledger_source_key"),
    )

    credit_event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True, index=True)
    subject_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject_storage_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Numeric(18, 6))
    source_key: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True, index=True)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class AdminAuditRecord(Base):
    __tablename__ = "admin_audit_log"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(96), index=True)
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class ProjectRecord(Base):
    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    owner: Mapped[UserRecord] = relationship(back_populates="projects")
    conversations: Mapped[list[ContractConversationRecord]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ContractConversationRecord(Base):
    __tablename__ = "contract_conversations"

    conversation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    display_name: Mapped[str] = mapped_column(String(240))
    research_request: Mapped[str] = mapped_column(Text, default="")
    research_problem: Mapped[str] = mapped_column(Text, default="")
    background_and_prior_progress: Mapped[str] = mapped_column(Text, default="")
    method_preferences_and_restrictions: Mapped[str] = mapped_column(Text, default="")
    valuable_partial_outcomes: Mapped[str] = mapped_column(Text, default="")
    possible_ideas_and_rough_approaches: Mapped[str] = mapped_column(Text, default="")
    contractor_profile: Mapped[str] = mapped_column(String(64), default="high")
    contract_language: Mapped[str] = mapped_column(String(16), default="zh")
    request_draft_revision: Mapped[int] = mapped_column(Integer, default=0)
    source_file_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    execution_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(48), default="request_draft", index=True)
    latest_contract_version: Mapped[int] = mapped_column(Integer, default=0)
    completed_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    owner: Mapped[UserRecord] = relationship(back_populates="conversations")
    project: Mapped[ProjectRecord | None] = relationship(back_populates="conversations")
    messages: Mapped[list[ChatMessageRecord]] = relationship(back_populates="conversation", cascade="all, delete-orphan", order_by="ChatMessageRecord.sequence")
    versions: Mapped[list[ContractVersionRecord]] = relationship(back_populates="conversation", cascade="all, delete-orphan", order_by="ContractVersionRecord.version")
    working_draft: Mapped[WorkingDraftRecord | None] = relationship(back_populates="conversation", cascade="all, delete-orphan", uselist=False)
    research_run: Mapped[ResearchRunRecord | None] = relationship(back_populates="conversation", cascade="all, delete-orphan", uselist=False)


class DeletionJobRecord(Base):
    """持久化终止并删除请求；不对目标建外键，确保目标消失后仍可审计。"""

    __tablename__ = "deletion_jobs"

    deletion_job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    target_type: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str] = mapped_column(String(64), index=True)
    target_name: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    claimed_by_runner: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatMessageRecord(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (UniqueConstraint("conversation_id", "sequence"),)

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("contract_conversations.conversation_id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(64), default="message")
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    conversation: Mapped[ContractConversationRecord] = relationship(back_populates="messages")


class ContractChatMessageRecord(Base):
    __tablename__ = "contract_chat_messages"
    __table_args__ = (UniqueConstraint("conversation_id", "sequence"),)

    chat_message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("contract_conversations.conversation_id", ondelete="CASCADE"), index=True)
    contract_version_id: Mapped[str] = mapped_column(
        ForeignKey("contract_versions.contract_version_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    draft_revision_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    draft_revision_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ContractGenerationAttemptRecord(Base):
    __tablename__ = "contract_generation_attempts"
    __table_args__ = (UniqueConstraint("conversation_id", "attempt_number"),)

    generation_attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("contract_conversations.conversation_id", ondelete="CASCADE"), index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32), default="initial")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    contractor_profile: Mapped[str] = mapped_column(String(64), default="high")
    request_snapshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    base_contract_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    base_draft_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    claimed_by_runner: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resulting_contract_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    reserved_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    precharged_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    actual_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    settled_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    runtime_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    runtime_cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    keep_runtime: Mapped[bool] = mapped_column(Boolean, default=False)
    runtime_cleanup_claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    runtime_cleanup_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runtime_cleanup_attempts: Mapped[int] = mapped_column(Integer, default=0)
    runtime_cleanup_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContractGenerationStageRecord(Base):
    __tablename__ = "contract_generation_stages"
    __table_args__ = (
        UniqueConstraint("generation_attempt_id", "stage_key"),
    )

    generation_stage_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    generation_attempt_id: Mapped[str] = mapped_column(
        ForeignKey("contract_generation_attempts.generation_attempt_id", ondelete="CASCADE"),
        index=True,
    )
    stage_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="waiting", index=True)
    provider_thread_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    repair_number: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ContractChatCallRecord(Base):
    """一次同步 Contract Chat 模型调用及其 Credit 预扣/结算。"""

    __tablename__ = "contract_chat_calls"

    chat_call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("contract_conversations.conversation_id", ondelete="CASCADE"), index=True)
    contract_version_id: Mapped[str] = mapped_column(ForeignKey("contract_versions.contract_version_id", ondelete="CASCADE"), index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    profile: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="reserved", index=True)
    expected_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    reserved_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    precharged_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    actual_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    settled_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContractVersionRecord(Base):
    __tablename__ = "contract_versions"
    __table_args__ = (UniqueConstraint("conversation_id", "version"),)

    contract_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("contract_conversations.conversation_id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    markdown_path: Mapped[str] = mapped_column(Text)
    markdown_sha256: Mapped[str] = mapped_column(String(64))
    sections: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    execution_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    contract_language: Mapped[str] = mapped_column(String(16), default="zh")
    created_by: Mapped[str] = mapped_column(String(32), default="contractor")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    conversation: Mapped[ContractConversationRecord] = relationship(back_populates="versions")


class WorkingDraftRecord(Base):
    __tablename__ = "working_drafts"

    conversation_id: Mapped[str] = mapped_column(ForeignKey("contract_conversations.conversation_id", ondelete="CASCADE"), primary_key=True)
    base_version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(240))
    sections: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    execution_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    contract_language: Mapped[str] = mapped_column(String(16), default="zh")
    draft_revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    conversation: Mapped[ContractConversationRecord] = relationship(back_populates="working_draft")


class FileAssetRecord(Base):
    __tablename__ = "file_assets"

    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    original_name: Mapped[str] = mapped_column(String(512))
    extension: Mapped[str] = mapped_column(String(32))
    media_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    artifact_path: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AgentThreadRecord(Base):
    __tablename__ = "agent_threads"
    __table_args__ = (UniqueConstraint("scope_type", "scope_id", "role"),)

    agent_thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(32), index=True)
    scope_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(96))
    provider_thread_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    persistent: Mapped[bool] = mapped_column(Boolean, default=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ResearchRunRecord(Base):
    __tablename__ = "research_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("contract_conversations.conversation_id", ondelete="CASCADE"), unique=True, index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), index=True)
    contract_version_id: Mapped[str] = mapped_column(ForeignKey("contract_versions.contract_version_id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(48), default="created", index=True)
    completion_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    graph_version: Mapped[int] = mapped_column(Integer, default=1)
    expected_cost: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    reserved_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    precharged_credit: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    actual_cost: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    settled_cost: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    execution_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    research_language: Mapped[str] = mapped_column(String(16), default="zh")
    claimed_by_runner: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_root: Mapped[str] = mapped_column(Text)
    contract_snapshot_path: Mapped[str] = mapped_column(Text)
    source_snapshot_path: Mapped[str] = mapped_column(Text)
    handoff_manifest_path: Mapped[str] = mapped_column(Text)
    final_report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_paper_pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_paper_tex_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    graceful_drain_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    graceful_drain_requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pause_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    pause_requested_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    pause_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pre_pause_status: Mapped[str | None] = mapped_column(String(48), nullable=True)
    pre_pause_phase: Mapped[str | None] = mapped_column(String(48), nullable=True)
    resume_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    total_paused_seconds: Mapped[float] = mapped_column(Float, default=0)
    pause_epoch: Mapped[int] = mapped_column(Integer, default=0)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    runtime_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    runtime_cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    keep_runtime: Mapped[bool] = mapped_column(Boolean, default=False)
    final_snapshot_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runtime_cleanup_claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    runtime_cleanup_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runtime_cleanup_attempts: Mapped[int] = mapped_column(Integer, default=0)
    runtime_cleanup_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    conversation: Mapped[ContractConversationRecord] = relationship(back_populates="research_run")
    attempts: Mapped[list[RunAttemptRecord]] = relationship(back_populates="run", cascade="all, delete-orphan", order_by="RunAttemptRecord.attempt_number")
    nodes: Mapped[list[ResearchNodeRecord]] = relationship(back_populates="run", cascade="all, delete-orphan")
    agent_operations: Mapped[list[AgentOperationCheckpointRecord]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class RunAttemptRecord(Base):
    __tablename__ = "run_attempts"
    __table_args__ = (UniqueConstraint("run_id", "attempt_number"),)

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(48), default="created")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    run: Mapped[ResearchRunRecord] = relationship(back_populates="attempts")


class AgentOperationCheckpointRecord(Base):
    """持久记录一次业务 Agent operation，供 Run 暂停后原地恢复。"""

    __tablename__ = "agent_operation_checkpoints"

    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_runs.run_id", ondelete="CASCADE"), index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("run_attempts.attempt_id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    operation_kind: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(96))
    role_key: Mapped[str] = mapped_column(String(128))
    scope_type: Mapped[str] = mapped_column(String(32))
    scope_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    provider_thread_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    task_path: Mapped[str] = mapped_column(Text)
    task_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    repair_number: Mapped[int] = mapped_column(Integer, default=0)
    pause_epoch: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[ResearchRunRecord] = relationship(back_populates="agent_operations")


class ResearchNodeRecord(Base):
    __tablename__ = "research_nodes"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id", ondelete="CASCADE"), index=True)
    parent_node_id: Mapped[str | None] = mapped_column(ForeignKey("research_nodes.node_id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(300))
    objective: Mapped[str] = mapped_column(Text)
    completion_criterion: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(48), default="ready", index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    origin_finding_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    origin_review_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    graph_version_created: Mapped[int] = mapped_column(Integer, default=1)
    node_version: Mapped[int] = mapped_column(Integer, default=1)
    local_review_count: Mapped[int] = mapped_column(Integer, default=0)
    final_stage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_stage_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    merged_into_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    run: Mapped[ResearchRunRecord] = relationship(back_populates="nodes")
    workers: Mapped[list[WorkerIdentityRecord]] = relationship(back_populates="node", cascade="all, delete-orphan")
    assignments: Mapped[list[WorkerAssignmentRecord]] = relationship(back_populates="node", cascade="all, delete-orphan")
    findings: Mapped[list[ResearchFindingRecord]] = relationship(back_populates="node", cascade="all, delete-orphan")


class ResearchGraphEdgeRecord(Base):
    __tablename__ = "research_graph_edges"
    __table_args__ = (UniqueConstraint("run_id", "source_node_id", "target_node_id", "relation"),)

    edge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id", ondelete="CASCADE"), index=True)
    source_node_id: Mapped[str] = mapped_column(ForeignKey("research_nodes.node_id", ondelete="CASCADE"), index=True)
    target_node_id: Mapped[str] = mapped_column(ForeignKey("research_nodes.node_id", ondelete="CASCADE"), index=True)
    relation: Mapped[str] = mapped_column(String(32))
    condition: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkerIdentityRecord(Base):
    __tablename__ = "worker_identities"
    __table_args__ = (UniqueConstraint("node_id", "worker_label"),)

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("research_nodes.node_id", ondelete="CASCADE"), index=True)
    persona: Mapped[str] = mapped_column(String(32), index=True)
    worker_label: Mapped[str] = mapped_column(String(96))
    status: Mapped[str] = mapped_column(String(32), default="idle", index=True)
    provider_thread_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    assignment_counter: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    node: Mapped[ResearchNodeRecord] = relationship(back_populates="workers")
    assignments: Mapped[list[WorkerAssignmentRecord]] = relationship(back_populates="worker", cascade="all, delete-orphan", order_by="WorkerAssignmentRecord.assignment_version")


class WorkerAssignmentRecord(Base):
    __tablename__ = "worker_assignments"
    __table_args__ = (UniqueConstraint("worker_id", "assignment_version"),)

    assignment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("research_nodes.node_id", ondelete="CASCADE"), index=True)
    worker_id: Mapped[str] = mapped_column(ForeignKey("worker_identities.worker_id", ondelete="CASCADE"), index=True)
    persona: Mapped[str] = mapped_column(String(32), index=True)
    worker_label: Mapped[str] = mapped_column(String(96))
    assignment_version: Mapped[int] = mapped_column(Integer)
    task_markdown: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(48), default="ready", index=True)
    report_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    node: Mapped[ResearchNodeRecord] = relationship(back_populates="assignments")
    worker: Mapped[WorkerIdentityRecord] = relationship(back_populates="assignments")
    reports: Mapped[list[WorkerReportRecord]] = relationship(back_populates="assignment", cascade="all, delete-orphan", order_by="WorkerReportRecord.report_number")


class WorkerReportRecord(Base):
    __tablename__ = "worker_reports"
    __table_args__ = (UniqueConstraint("assignment_id", "report_number"),)

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assignment_id: Mapped[str] = mapped_column(ForeignKey("worker_assignments.assignment_id", ondelete="CASCADE"), index=True)
    report_number: Mapped[int] = mapped_column(Integer)
    assignment_version: Mapped[int] = mapped_column(Integer)
    node_version: Mapped[int] = mapped_column(Integer)
    graph_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    major_event: Mapped[bool] = mapped_column(Boolean, default=False)
    markdown_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    consumption_status: Mapped[str] = mapped_column(
        String(32), default="pending_supervisor_review", index=True
    )
    consumed_by_supervisor_review_id: Mapped[str | None] = mapped_column(
        String(96), nullable=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    assignment: Mapped[WorkerAssignmentRecord] = relationship(back_populates="reports")


class ResearchFindingRecord(Base):
    __tablename__ = "research_findings"

    finding_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("research_nodes.node_id", ondelete="CASCADE"), index=True)
    source_report_id: Mapped[str | None] = mapped_column(ForeignKey("worker_reports.report_id", ondelete="SET NULL"), nullable=True)
    finding_type: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(300))
    markdown: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="local", index=True)
    significance: Mapped[str] = mapped_column(String(32), default="normal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    node: Mapped[ResearchNodeRecord] = relationship(back_populates="findings")
    proposal: Mapped[PromotionProposalRecord | None] = relationship(back_populates="finding", cascade="all, delete-orphan", uselist=False)


class PromotionProposalRecord(Base):
    __tablename__ = "promotion_proposals"

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    finding_id: Mapped[str] = mapped_column(ForeignKey("research_findings.finding_id", ondelete="CASCADE"), unique=True)
    supervisor_rationale: Mapped[str] = mapped_column(Text)
    recommended_actions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    finding: Mapped[ResearchFindingRecord] = relationship(back_populates="proposal")


class ArchitectureReviewRecord(Base):
    __tablename__ = "architecture_reviews"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)

    review_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(160))
    graph_version_before: Mapped[int] = mapped_column(Integer)
    graph_version_after: Mapped[int] = mapped_column(Integer)
    promotion_slots: Mapped[int] = mapped_column(Integer)
    decision_markdown_path: Mapped[str] = mapped_column(Text)
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    provider_thread_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    trigger_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_snapshot_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_node_preconditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    application_status: Mapped[str] = mapped_column(String(32), default="applied")
    input_delta_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_communication_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence_index_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_index_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_inspected: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    snapshot_update_markdown: Mapped[str] = mapped_column(Text, default="")
    ledger_updates_markdown: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ArchitectureSnapshotRecord(Base):
    """保存每个 Run 的当前全局白板，而不是累积 Reviewer 对话。"""

    __tablename__ = "architecture_snapshots"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    graph_version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    snapshot_markdown_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    last_review_research_seconds: Mapped[float] = mapped_column(Float, default=0)
    last_root_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class GlobalStateDeltaRecord(Base):
    """持久化 Reviewer 尚未消费的客观状态变化。"""

    __tablename__ = "global_state_deltas"

    delta_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_runs.run_id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    node_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graph_version: Mapped[int] = mapped_column(Integer)
    change_kind: Mapped[str] = mapped_column(String(64), index=True)
    old_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    new_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    gap_key: Mapped[str | None] = mapped_column(String(300), nullable=True, index=True)
    root_relevant: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    consumed_by_review_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ArchitectureCommunicationRecord(Base):
    """保存 Supervisor 提交给 fresh Reviewer 的显式全局决策问题。"""

    __tablename__ = "architecture_communications"

    communication_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_runs.run_id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("research_nodes.node_id", ondelete="CASCADE"), index=True
    )
    node_version: Mapped[int] = mapped_column(Integer)
    graph_version: Mapped[int] = mapped_column(Integer)
    attention: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(300))
    question: Mapped[str] = mapped_column(Text)
    global_consequence: Mapped[str] = mapped_column(Text)
    affected_node_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    related_evidence_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    local_pressure_tests: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    source_markdown_path: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    consumed_by_review_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_by_local_review_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ArchitectureDecisionLedgerRecord(Base):
    """追加保存未来仍必须尊重的稀疏全局治理决定。"""

    __tablename__ = "architecture_decision_ledger"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)

    ledger_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_runs.run_id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    source_review_id: Mapped[str] = mapped_column(String(64), index=True)
    graph_version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(300))
    markdown: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class VerifiedClaimRecord(Base):
    __tablename__ = "verified_claims"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "candidate_artifact_id",
            name="uq_verified_claims_run_candidate_artifact",
        ),
    )

    verified_claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.run_id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("research_nodes.node_id", ondelete="CASCADE"), index=True)
    candidate_artifact_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    verdict: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(300), default="Verified mathematical result")
    statement: Mapped[str] = mapped_column(Text)
    statement_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proof_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verifier_report_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_scope: Mapped[str] = mapped_column(String(32), default="auxiliary_fact")
    controlling_proposition_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reuse_boundary: Mapped[str] = mapped_column(Text, default="")
    node_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    graph_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verification_policy_version: Mapped[str] = mapped_column(String(64), default="claim-v2")
    certificate_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    verifier_report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class UsageLedgerRecord(Base):
    __tablename__ = "usage_ledger"

    usage_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    component: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(96))
    model: Mapped[str] = mapped_column(String(128))
    provider_thread_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    usage_semantics: Mapped[str] = mapped_column(String(32), default="per_turn")
    normalization_version: Mapped[str] = mapped_column(String(32), default="turn-v1")
    reported_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reported_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reported_cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class RuntimeEventRecord(Base):
    __tablename__ = "runtime_events"
    __table_args__ = (UniqueConstraint("scope_type", "scope_id", "sequence"),)

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(32), index=True)
    scope_id: Mapped[str] = mapped_column(String(64), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ScopedSequenceRecord(Base):
    """Allocate monotone sequences atomically for one logical namespace and scope."""

    __tablename__ = "scoped_sequences"

    namespace: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    next_value: Mapped[int] = mapped_column(BigInteger, default=1)


class PublicationStateRecord(Base):
    """The authoritative single-flight state for one Run publication attempt."""

    __tablename__ = "publication_states"
    __table_args__ = (UniqueConstraint("run_id", "attempt_id"),)

    publication_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("research_runs.run_id", ondelete="CASCADE"), index=True
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("run_attempts.attempt_id", ondelete="CASCADE"), index=True
    )
    completion_kind: Mapped[str] = mapped_column(String(48))
    source: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="authorized", index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    publishing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class RunnerHeartbeatRecord(Base):
    __tablename__ = "runner_heartbeats"

    runner_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="idle")
    current_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Database:
    """SQLAlchemy Engine / Session wrapper, with SQLite compatibility for tests."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        connect_args: dict[str, Any] = {}
        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        self.engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
        if url.startswith("sqlite"):
            self._enable_sqlite_foreign_keys(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, class_=Session, expire_on_commit=False, future=True)

    @staticmethod
    def _enable_sqlite_foreign_keys(engine: Engine) -> None:
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
