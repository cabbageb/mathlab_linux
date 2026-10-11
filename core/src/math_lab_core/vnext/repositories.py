from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable
import hashlib

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, selectinload

from .artifacts import ArtifactStore, sha256_file
from .db import (
    AdminAuditRecord,
    AgentOperationCheckpointRecord,
    AgentThreadRecord,
    AllowedEmailDomainRecord,
    AuthSessionRecord,
    ArchitectureCommunicationRecord,
    ArchitectureDecisionLedgerRecord,
    ArchitectureReviewRecord,
    ArchitectureSnapshotRecord,
    ChatMessageRecord,
    ContractChatMessageRecord,
    ContractChatCallRecord,
    ContractGenerationAttemptRecord,
    ContractGenerationStageRecord,
    ContractConversationRecord,
    ContractVersionRecord,
    CreditLedgerRecord,
    DeletionJobRecord,
    EmailDomainGroupRuleRecord,
    EmailOtpRecord,
    EmailOtpSendReservationRecord,
    FileAssetRecord,
    GroupMembershipRecord,
    GroupRecord,
    GlobalStateDeltaRecord,
    ProjectRecord,
    PromotionProposalRecord,
    PublicationStateRecord,
    ResearchFindingRecord,
    ResearchGraphEdgeRecord,
    ResearchNodeRecord,
    ResearchRunRecord,
    RunAttemptRecord,
    RunnerHeartbeatRecord,
    RuntimeEventRecord,
    ScopedSequenceRecord,
    UsageLedgerRecord,
    UserRecord,
    UserStorageSequenceRecord,
    VerifiedClaimRecord,
    WorkerAssignmentRecord,
    WorkerIdentityRecord,
    WorkerReportRecord,
    WorkingDraftRecord,
    new_id,
    utc_now,
)
from .languages import normalize_language


class OwnershipError(PermissionError):
    pass


class RepositoryHub:
    """把数据库结构隐藏在稳定业务接口后，Planner/Prover 不直接拼 SQL。"""

    def __init__(self, session: Session, artifacts: ArtifactStore) -> None:
        self.session = session
        self.artifacts = artifacts

    # ---------- users ----------
    @staticmethod
    def _credit(value: float | int | Decimal | None) -> Decimal:
        return Decimal(str(value or 0)).quantize(Decimal("0.000001"))

    def _next_scoped_sequence(
        self, namespace: str, scope_id: str, *, initial_value: int = 1
    ) -> int:
        """Atomically allocate one monotone sequence value in SQLite or PostgreSQL."""
        bind = self.session.get_bind()
        values = {
            "namespace": namespace,
            "scope_id": scope_id,
            "next_value": max(1, int(initial_value)),
        }
        if bind.dialect.name == "postgresql":
            statement = postgresql_insert(ScopedSequenceRecord).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=["namespace", "scope_id"]
            )
        elif bind.dialect.name == "sqlite":
            statement = sqlite_insert(ScopedSequenceRecord).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=["namespace", "scope_id"]
            )
        else:  # pragma: no cover - production and tests use PostgreSQL/SQLite
            counter = self.session.get(ScopedSequenceRecord, (namespace, scope_id))
            if counter is None:
                counter = ScopedSequenceRecord(**values)
                self.session.add(counter)
                self.session.flush()
            result = int(counter.next_value)
            counter.next_value = result + 1
            self.session.flush()
            return result
        self.session.execute(statement)
        value = self.session.scalar(
            update(ScopedSequenceRecord)
            .where(
                ScopedSequenceRecord.namespace == namespace,
                ScopedSequenceRecord.scope_id == scope_id,
            )
            .values(next_value=ScopedSequenceRecord.next_value + 1)
            .returning(ScopedSequenceRecord.next_value)
        )
        if value is None:
            raise RuntimeError("scoped sequence 分配失败")
        return int(value) - 1

    def allocate_storage_key(self, preferred: str | None = None) -> str:
        """用单行计数器分配稳定 storage_key；PostgreSQL 下锁行，SQLite 写事务串行。"""
        statement = select(UserStorageSequenceRecord).where(
            UserStorageSequenceRecord.sequence_name == "users"
        )
        if self.session.bind is not None and self.session.bind.dialect.name != "sqlite":
            statement = statement.with_for_update()
        counter = self.session.scalar(statement)
        if counter is None:
            counter = UserStorageSequenceRecord(sequence_name="users", next_value=1)
            self.session.add(counter)
            self.session.flush()
        if preferred:
            if not preferred.startswith("user_") or not preferred[5:].isdigit():
                raise ValueError("storage_key 必须使用 user_000001 格式")
            number = int(preferred[5:])
            existing = self.session.scalar(
                select(UserRecord.user_id).where(UserRecord.storage_key == preferred)
            )
            if existing is not None:
                raise ValueError(f"storage_key 已占用：{preferred}")
            counter.next_value = max(int(counter.next_value), number + 1)
            self.session.flush()
            return preferred
        number = int(counter.next_value)
        counter.next_value = number + 1
        self.session.flush()
        return f"user_{number:06d}"

    def get_user_any_status(self, user_id: str) -> UserRecord:
        item = self.session.get(UserRecord, user_id)
        if item is None:
            raise FileNotFoundError(f"用户不存在：{user_id}")
        return item

    def find_user_by_email(self, email: str) -> UserRecord | None:
        return self.session.scalar(
            select(UserRecord).where(UserRecord.email == email.strip().lower())
        )

    def ensure_identity_policies(self) -> None:
        """幂等建立本地/External 系统 Group 与可选域名策略。"""
        groups: dict[str, GroupRecord] = {}
        for key, name, description in (
            ("institution", "Institution", "Automatically assigned to verified institutional email addresses."),
            ("external", "External", "Automatically assigned to other verified email addresses."),
        ):
            group = self.session.scalar(select(GroupRecord).where(GroupRecord.system_key == key))
            if group is None:
                group = GroupRecord(
                    group_id=new_id("group"), system_key=key, name=name,
                    description=description, status="active",
                )
                self.session.add(group)
                self.session.flush()
            groups[key] = group
        for domain in ():
            rule = self.session.scalar(
                select(EmailDomainGroupRuleRecord).where(
                    EmailDomainGroupRuleRecord.domain == domain
                )
            )
            if rule is None:
                self.session.add(
                    EmailDomainGroupRuleRecord(
                        rule_id=new_id("domainrule"), domain=domain,
                        group_id=groups["institution"].group_id,
                        welcome_credit=self._credit(50),
                        grant_key="institution_welcome_v1", enabled=True,
                    )
                )
        self.session.flush()

    def ensure_dev_users(self) -> list[UserRecord]:
        """建立真实 schema 的本地测试用户，后续邮箱认证只替换 AuthContext。"""
        seeds = (
            ("dev_admin", "admin@local.mathlab", "MathLab Admin", "admin"),
            ("dev_user_a", "alice@local.mathlab", "Alice · DEV", "user"),
            ("dev_user_b", "bob@local.mathlab", "Bob · DEV", "user"),
        )
        result: list[UserRecord] = []
        for user_id, email, name, role in seeds:
            item = self.session.get(UserRecord, user_id)
            if item is None:
                item = UserRecord(
                    user_id=user_id, storage_key=self.allocate_storage_key(),
                    email=email,
                    display_name=name,
                    role=role,
                    status="active",
                    email_verified_at=utc_now(),
                    credit_balance=self._credit(1000 if role == "admin" else 100),
                    max_concurrent_research_runs=10 if role == "admin" else 2,
                )
                self.session.add(item)
                self.session.flush()
            result.append(item)
        return result

    def list_users(self, email_query: str | None = None) -> list[UserRecord]:
        statement = select(UserRecord)
        if email_query and email_query.strip():
            statement = statement.where(
                func.lower(UserRecord.email).contains(email_query.strip().lower())
            )
        return list(self.session.scalars(statement.order_by(UserRecord.created_at)))

    def get_user(self, user_id: str) -> UserRecord:
        item = self.get_user_any_status(user_id)
        if item is None or item.status != "active":
            raise FileNotFoundError(f"用户不存在或不可用：{user_id}")
        return item

    def update_user_preferences(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
        institution: str | None = None,
        department: str | None = None,
        position: str | None = None,
        default_model_strategy: str | None = None,
        default_budget_preset: str | None = None,
        website_language: str | None = None,
        default_contract_language: str | None = None,
        default_research_language: str | None = None,
        contract_chat_panel_width: float | None = None,
    ) -> UserRecord:
        """保存当前用户偏好与可选单位资料；空字符串清空资料，未提供的字段保持原值。"""
        user = self.get_user(user_id)
        if display_name is not None and display_name.strip():
            user.display_name = display_name.strip()
        for field, value in (("institution", institution), ("department", department), ("position", position)):
            if value is not None:
                setattr(user, field, value.strip() or None)
        if default_model_strategy is not None:
            user.default_model_strategy = default_model_strategy.strip() or user.default_model_strategy
        if default_budget_preset is not None:
            user.default_budget_preset = default_budget_preset.strip() or user.default_budget_preset
        if website_language is not None:
            user.website_language = normalize_language(website_language)
        if default_contract_language is not None:
            user.default_contract_language = normalize_language(default_contract_language)
        if default_research_language is not None:
            user.default_research_language = normalize_language(default_research_language)
        if contract_chat_panel_width is not None:
            user.contract_chat_panel_width = max(0.22, min(0.55, float(contract_chat_panel_width)))
        user.updated_at = utc_now()
        self.session.flush()
        return user

    # ---------- authentication / email domains ----------
    def ensure_allowed_domains(self, domains: Iterable[str], *, source: str = "config") -> list[AllowedEmailDomainRecord]:
        result: list[AllowedEmailDomainRecord] = []
        for raw in domains:
            domain = raw.strip().lower().lstrip("@")
            if not domain:
                continue
            item = self.session.scalar(select(AllowedEmailDomainRecord).where(AllowedEmailDomainRecord.domain == domain))
            if item is None:
                item = AllowedEmailDomainRecord(domain_id=new_id("domain"), domain=domain, enabled=True, source=source)
                self.session.add(item)
                self.session.flush()
            result.append(item)
        return result

    def list_allowed_domains(self) -> list[AllowedEmailDomainRecord]:
        return list(self.session.scalars(select(AllowedEmailDomainRecord).order_by(AllowedEmailDomainRecord.domain)))

    def is_email_allowed(self, email: str) -> bool:
        normalized = email.strip().lower()
        if "@" not in normalized:
            return False
        domain = normalized.rsplit("@", 1)[1]
        return bool(self.session.scalar(select(AllowedEmailDomainRecord.domain_id).where(
            AllowedEmailDomainRecord.domain == domain,
            AllowedEmailDomainRecord.enabled.is_(True),
        )))

    def upsert_allowed_domain(self, actor_user_id: str, domain: str, enabled: bool = True) -> AllowedEmailDomainRecord:
        actor = self.get_user(actor_user_id)
        if actor.role != "admin":
            raise OwnershipError("只有管理员可以修改邮箱域名白名单")
        normalized = domain.strip().lower().lstrip("@")
        if not normalized or "." not in normalized:
            raise ValueError("邮箱域名格式无效")
        item = self.session.scalar(select(AllowedEmailDomainRecord).where(AllowedEmailDomainRecord.domain == normalized))
        if item is None:
            item = AllowedEmailDomainRecord(
                domain_id=new_id("domain"), domain=normalized, enabled=enabled,
                source="admin", created_by=actor_user_id,
            )
            self.session.add(item)
        else:
            item.enabled = enabled
            item.updated_at = utc_now()
        self.add_admin_audit(actor_user_id, "email_domain_upsert", "email_domain", item.domain_id, {"domain": normalized, "enabled": enabled})
        self.session.flush()
        return item

    def delete_allowed_domain(self, actor_user_id: str, domain_id: str) -> None:
        self.require_admin(actor_user_id)
        item = self.session.get(AllowedEmailDomainRecord, domain_id)
        if item is None:
            raise FileNotFoundError("邮箱域名不存在")
        self.add_admin_audit(actor_user_id, "email_domain_delete", "email_domain", domain_id, {"domain": item.domain})
        self.session.delete(item)
        self.session.flush()

    def list_domain_group_rules(self) -> list[EmailDomainGroupRuleRecord]:
        return list(self.session.scalars(
            select(EmailDomainGroupRuleRecord).order_by(EmailDomainGroupRuleRecord.domain)
        ))

    def upsert_domain_group_rule(
        self,
        actor_user_id: str,
        *,
        domain: str,
        group_id: str,
        welcome_credit: float,
        enabled: bool,
        grant_key: str | None = None,
    ) -> EmailDomainGroupRuleRecord:
        self.require_admin(actor_user_id)
        normalized = domain.strip().lower().lstrip("@")
        if not normalized or "." not in normalized:
            raise ValueError("邮箱域名格式无效")
        if self.session.get(GroupRecord, group_id) is None:
            raise FileNotFoundError("Group 不存在")
        item = self.session.scalar(select(EmailDomainGroupRuleRecord).where(
            EmailDomainGroupRuleRecord.domain == normalized
        ))
        if item is None:
            item = EmailDomainGroupRuleRecord(
                rule_id=new_id("domainrule"), domain=normalized, group_id=group_id,
                welcome_credit=self._credit(welcome_credit),
                grant_key=(grant_key or f"domain_welcome_{normalized}").strip(),
                enabled=bool(enabled),
            )
            self.session.add(item)
        else:
            item.group_id = group_id; item.welcome_credit = self._credit(welcome_credit)
            item.grant_key = (grant_key or item.grant_key).strip(); item.enabled = bool(enabled)
            item.updated_at = utc_now()
        self.add_admin_audit(actor_user_id, "domain_group_rule_upsert", "domain_rule", item.rule_id, {
            "domain": normalized, "group_id": group_id,
            "welcome_credit": float(welcome_credit), "enabled": bool(enabled),
        })
        self.session.flush()
        return item

    def delete_domain_group_rule(self, actor_user_id: str, rule_id: str) -> None:
        self.require_admin(actor_user_id)
        item = self.session.get(EmailDomainGroupRuleRecord, rule_id)
        if item is None:
            raise FileNotFoundError("邮箱域名策略不存在")
        self.add_admin_audit(actor_user_id, "domain_group_rule_delete", "domain_rule", rule_id, {"domain": item.domain})
        self.session.delete(item); self.session.flush()

    def create_otp(self, email: str, code_hash: str, salt: str, expires_at: datetime, max_attempts: int) -> EmailOtpRecord:
        """Create a non-usable OTP until the mail transport confirms delivery."""
        normalized = email.strip().lower()
        otp = EmailOtpRecord(
            otp_id=new_id("otp"), email=normalized, code_hash=code_hash, salt=salt,
            expires_at=expires_at, max_attempts=max_attempts, status="pending_delivery",
        )
        self.session.add(otp)
        self.session.flush()
        return otp

    def reserve_otp_send(
        self,
        email: str,
        *,
        now: datetime,
        cooldown_seconds: int,
        daily_limit: int,
    ) -> EmailOtpSendReservationRecord:
        """Reserve one email send before SMTP so concurrent requests cannot pass together."""
        normalized = email.strip().lower()
        bind = self.session.get_bind()
        values = {"email": normalized, "otp_id": None, "reserved_at": None, "updated_at": now}
        if bind.dialect.name == "postgresql":
            statement = postgresql_insert(EmailOtpSendReservationRecord).values(**values)
            statement = statement.on_conflict_do_nothing(index_elements=["email"])
        elif bind.dialect.name == "sqlite":
            statement = sqlite_insert(EmailOtpSendReservationRecord).values(**values)
            statement = statement.on_conflict_do_nothing(index_elements=["email"])
        else:  # pragma: no cover
            statement = None
        if statement is not None:
            self.session.execute(statement)
        query = select(EmailOtpSendReservationRecord).where(
            EmailOtpSendReservationRecord.email == normalized
        )
        if bind.dialect.name != "sqlite":
            query = query.with_for_update()
        reservation = self.session.scalar(query)
        if reservation is None:  # pragma: no cover
            reservation = EmailOtpSendReservationRecord(**values)
            self.session.add(reservation)
            self.session.flush()
        if reservation.reserved_at is not None:
            reserved_at = reservation.reserved_at
            if reserved_at.tzinfo is None:
                reserved_at = reserved_at.replace(tzinfo=timezone.utc)
            elapsed = (now - reserved_at).total_seconds()
            if elapsed < cooldown_seconds:
                wait = max(1, int(cooldown_seconds - elapsed))
                raise ValueError(f"验证码发送请求正在处理中，请 {wait} 秒后再试")
        delivered_today = self.count_successful_otp_deliveries(
            normalized, now - timedelta(hours=24)
        )
        if delivered_today >= daily_limit:
            raise ValueError("该邮箱过去 24 小时发送验证码次数已达上限")
        latest = self.latest_otp(normalized)
        if latest is not None:
            delivered_at = latest.delivered_at or latest.created_at
            if delivered_at.tzinfo is None:
                delivered_at = delivered_at.replace(tzinfo=timezone.utc)
            elapsed = (now - delivered_at).total_seconds()
            if elapsed < cooldown_seconds:
                wait = max(1, int(cooldown_seconds - elapsed))
                raise ValueError(f"验证码发送过于频繁，请 {wait} 秒后再试")
        reservation.reserved_at = now
        reservation.updated_at = now
        self.session.flush()
        return reservation

    def bind_otp_send_reservation(self, email: str, otp_id: str) -> None:
        """Bind an already-created OTP to its serialized delivery reservation."""
        reservation = self.session.get(EmailOtpSendReservationRecord, email.strip().lower())
        if reservation is None or reservation.reserved_at is None:
            raise RuntimeError("OTP send reservation 不存在")
        reservation.otp_id = otp_id
        reservation.updated_at = utc_now()
        self.session.flush()

    def release_otp_send_reservation(self, email: str, otp_id: str) -> None:
        """Release only the reservation owned by this OTP delivery attempt."""
        reservation = self.session.get(EmailOtpSendReservationRecord, email.strip().lower())
        if reservation is None or reservation.otp_id != otp_id:
            return
        reservation.otp_id = None
        reservation.reserved_at = None
        reservation.updated_at = utc_now()
        self.session.flush()

    def mark_otp_delivered(self, otp_id: str) -> EmailOtpRecord:
        otp = self.session.get(EmailOtpRecord, otp_id)
        if otp is None:
            raise FileNotFoundError("OTP 不存在")
        now = utc_now()
        # Only a successfully delivered newest code should remain usable. A failed
        # delivery must never revoke the user's previous delivered code.
        previous = list(self.session.scalars(select(EmailOtpRecord).where(
            EmailOtpRecord.email == otp.email,
            EmailOtpRecord.otp_id != otp.otp_id,
            EmailOtpRecord.used_at.is_(None),
            EmailOtpRecord.status.in_(["pending_delivery", "delivered"]),
        )))
        for item in previous:
            item.status = "revoked"
            item.used_at = now
        otp.status = "delivered"
        otp.delivered_at = now
        otp.delivery_error_class = None
        self.session.flush()
        return otp

    def mark_otp_delivery_failed(self, otp_id: str, error_class: str) -> EmailOtpRecord:
        otp = self.session.get(EmailOtpRecord, otp_id)
        if otp is None:
            raise FileNotFoundError("OTP 不存在")
        otp.status = "delivery_failed"
        otp.delivery_error_class = (error_class or "SMTPError")[:160]
        otp.used_at = utc_now()
        self.session.flush()
        return otp

    def latest_otp(self, email: str) -> EmailOtpRecord | None:
        """Return the newest *delivered* and unused OTP."""
        return self.session.scalar(select(EmailOtpRecord).where(
            EmailOtpRecord.email == email.strip().lower(),
            EmailOtpRecord.status == "delivered",
            EmailOtpRecord.used_at.is_(None),
        ).order_by(EmailOtpRecord.delivered_at.desc(), EmailOtpRecord.created_at.desc()).limit(1))

    def latest_otp_attempt(self, email: str) -> EmailOtpRecord | None:
        return self.session.scalar(select(EmailOtpRecord).where(
            EmailOtpRecord.email == email.strip().lower(),
        ).order_by(EmailOtpRecord.created_at.desc()).limit(1))

    def get_or_create_email_user(
        self,
        email: str,
        *,
        initial_credit: float = 0.0,
        bootstrap_admin_email: str = "",
        bootstrap_admin_credit: float = 0.0,
        default_storage_quota_bytes: int = 536_870_912,
        bootstrap_admin_max_concurrent_runs: int = 10,
        default_user_max_concurrent_runs: int = 2,
    ) -> UserRecord:
        normalized = email.strip().lower()
        item = self.session.scalar(select(UserRecord).where(UserRecord.email == normalized))
        if item is None:
            local = normalized.split("@", 1)[0]
            is_bootstrap_admin = bool(bootstrap_admin_email and normalized == bootstrap_admin_email.strip().lower())
            starting_credit = float(bootstrap_admin_credit if is_bootstrap_admin else initial_credit)
            item = UserRecord(
                user_id=new_id("user"), storage_key=self.allocate_storage_key(
                    "user_000001" if is_bootstrap_admin else None
                ), email=normalized, display_name=local,
                role="admin" if is_bootstrap_admin else "user",
                is_developer=is_bootstrap_admin,
                status="active", email_verified_at=utc_now(),
                credit_balance=self._credit(starting_credit),
                storage_quota_bytes=None if is_bootstrap_admin else int(default_storage_quota_bytes),
                max_concurrent_research_runs=(
                    max(1, int(bootstrap_admin_max_concurrent_runs))
                    if is_bootstrap_admin else max(1, int(default_user_max_concurrent_runs))
                ),
            )
            self.session.add(item)
            self.session.flush()
            if starting_credit:
                self.session.add(CreditLedgerRecord(
                    credit_event_id=new_id("credit"), user_id=item.user_id,
                    subject_email=item.email, subject_storage_key=item.storage_key,
                    amount=self._credit(starting_credit),
                    reason="bootstrap admin credit" if is_bootstrap_admin else "initial signup credit",
                    created_by=None, source_key="bootstrap_admin_v1" if is_bootstrap_admin else f"initial_signup:{item.user_id}",
                ))
        else:
            item.email_verified_at = item.email_verified_at or utc_now()
            if item.status == "disabled":
                raise OwnershipError("该用户已被管理员禁用")
            if bootstrap_admin_email and normalized == bootstrap_admin_email.strip().lower():
                item.role = "admin"; item.is_developer = True
                item.max_concurrent_research_runs = max(1, int(bootstrap_admin_max_concurrent_runs))
            else:
                item.max_concurrent_research_runs = (
                    max(1, int(bootstrap_admin_max_concurrent_runs))
                    if item.role == "admin"
                    else max(1, int(default_user_max_concurrent_runs))
                )
            item.status = "active"
            if not item.storage_key:
                item.storage_key = self.allocate_storage_key(
                    "user_000001" if bootstrap_admin_email and normalized == bootstrap_admin_email.strip().lower() else None
                )
        self.ensure_identity_policies()
        self._apply_verified_email_policy(
            item,
            skip_welcome=bool(bootstrap_admin_email and normalized == bootstrap_admin_email.strip().lower()),
        )
        self.session.flush()
        return item

    def _apply_verified_email_policy(self, user: UserRecord, *, skip_welcome: bool) -> None:
        domain = str(user.email or "").rsplit("@", 1)[-1].lower()
        rule = self.session.scalar(
            select(EmailDomainGroupRuleRecord).where(
                EmailDomainGroupRuleRecord.domain == domain,
                EmailDomainGroupRuleRecord.enabled.is_(True),
            )
        )
        if rule is None:
            group = self.session.scalar(select(GroupRecord).where(GroupRecord.system_key == "external"))
            welcome = self._credit(0); grant_key = "external_signup_v1"
        else:
            group = self.session.get(GroupRecord, rule.group_id)
            welcome = self._credit(rule.welcome_credit); grant_key = rule.grant_key
        if group is not None:
            existing = self.session.scalar(select(GroupMembershipRecord).where(
                GroupMembershipRecord.group_id == group.group_id,
                GroupMembershipRecord.user_id == user.user_id,
            ))
            if existing is None:
                self.session.add(GroupMembershipRecord(
                    membership_id=new_id("membership"), group_id=group.group_id,
                    user_id=user.user_id,
                ))
        source_key = f"{grant_key}:{user.user_id}"
        already = self.session.scalar(
            select(CreditLedgerRecord.credit_event_id).where(
                CreditLedgerRecord.source_key == source_key
            )
        )
        if not skip_welcome and welcome > 0 and already is None:
            user.credit_balance = self._credit(user.credit_balance) + welcome
            self.session.add(CreditLedgerRecord(
                credit_event_id=new_id("credit"), user_id=user.user_id,
                subject_email=user.email, subject_storage_key=user.storage_key,
                amount=welcome, reason="Institution welcome credit",
                source_key=source_key,
            ))

    def count_successful_otp_deliveries(self, email: str, since: datetime) -> int:
        return int(self.session.scalar(select(func.count()).select_from(EmailOtpRecord).where(
            EmailOtpRecord.email == email.strip().lower(),
            EmailOtpRecord.status == "delivered",
            EmailOtpRecord.delivered_at >= since,
        )) or 0)

    def create_auth_session(self, user_id: str, session_id: str, expires_at: datetime) -> AuthSessionRecord:
        self.get_user(user_id)
        item = AuthSessionRecord(session_id=session_id, user_id=user_id, expires_at=expires_at)
        self.session.add(item)
        self.session.flush()
        return item

    def resolve_auth_session(self, session_id: str) -> UserRecord:
        item = self.session.get(AuthSessionRecord, session_id)
        now = utc_now()
        expires_at = item.expires_at if item is not None else now
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if item is None or item.revoked_at is not None or expires_at <= now:
            raise OwnershipError("登录会话无效或已过期")
        last_seen = item.last_seen_at
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if (now - last_seen).total_seconds() >= 300:
            item.last_seen_at = now
        return self.get_user(item.user_id)

    def revoke_auth_session(self, session_id: str) -> None:
        item = self.session.get(AuthSessionRecord, session_id)
        if item is not None:
            item.revoked_at = utc_now()

    def revoke_all_auth_sessions(self, user_id: str) -> int:
        rows = list(self.session.scalars(select(AuthSessionRecord).where(
            AuthSessionRecord.user_id == user_id,
            AuthSessionRecord.revoked_at.is_(None),
        )))
        now = utc_now()
        for item in rows:
            item.revoked_at = now
        self.session.flush()
        return len(rows)

    # ---------- groups / admin / credits ----------
    def add_admin_audit(self, actor_user_id: str | None, action: str, target_type: str, target_id: str | None, payload: dict[str, Any] | None = None) -> AdminAuditRecord:
        item = AdminAuditRecord(
            audit_id=new_id("audit"), actor_user_id=actor_user_id, action=action,
            target_type=target_type, target_id=target_id, payload=payload or {},
        )
        self.session.add(item)
        self.session.flush()
        return item

    def require_admin(self, user_id: str) -> UserRecord:
        user = self.get_user(user_id)
        if user.role != "admin":
            raise OwnershipError("需要管理员权限")
        return user

    def require_developer(self, user_id: str) -> UserRecord:
        user = self.get_user(user_id)
        if not user.is_developer:
            raise OwnershipError("需要 Developer 权限")
        return user

    def create_group(self, actor_user_id: str, name: str, description: str = "") -> GroupRecord:
        self.require_admin(actor_user_id)
        item = GroupRecord(group_id=new_id("group"), name=name.strip(), description=description.strip())
        self.session.add(item)
        self.session.flush()
        self.add_admin_audit(actor_user_id, "group_create", "group", item.group_id, {"name": item.name})
        return item

    def list_groups(self) -> list[GroupRecord]:
        return list(self.session.scalars(select(GroupRecord).order_by(GroupRecord.name)))

    def group_membership_is_automatic(self, group_id: str) -> bool:
        """判断 Group 成员是否由系统身份或启用的邮箱域规则自动维护。"""
        group = self.session.get(GroupRecord, group_id)
        if group is None:
            raise FileNotFoundError("Group 不存在")
        if group.system_key:
            return True
        rule_id = self.session.scalar(
            select(EmailDomainGroupRuleRecord.rule_id).where(
                EmailDomainGroupRuleRecord.group_id == group_id,
                EmailDomainGroupRuleRecord.enabled.is_(True),
            ).limit(1)
        )
        return rule_id is not None

    @staticmethod
    def _apply_group_defaults(group: GroupRecord, user: UserRecord) -> None:
        """在新增成员时沿用现有 Group 默认额度、并发与模型策略。"""
        if group.default_monthly_cost_limit is not None:
            user.monthly_cost_limit = float(group.default_monthly_cost_limit)
        if group.default_max_concurrent_runs is not None:
            user.max_concurrent_research_runs = max(1, int(group.default_max_concurrent_runs))
        if group.default_model_strategy:
            user.default_model_strategy = group.default_model_strategy
        if group.default_budget_preset:
            user.default_budget_preset = group.default_budget_preset
        user.updated_at = utc_now()

    def add_group_member(self, actor_user_id: str, group_id: str, user_id: str) -> GroupMembershipRecord:
        self.require_admin(actor_user_id)
        group = self.session.get(GroupRecord, group_id)
        if group is None:
            raise FileNotFoundError("Group 不存在")
        if self.group_membership_is_automatic(group_id):
            raise ValueError("自动 Group 的成员由邮箱策略维护，不能手动修改")
        user = self.get_user_any_status(user_id)
        existing = self.session.scalar(select(GroupMembershipRecord).where(
            GroupMembershipRecord.group_id == group_id,
            GroupMembershipRecord.user_id == user_id,
        ))
        if existing is not None:
            return existing
        item = GroupMembershipRecord(membership_id=new_id("membership"), group_id=group_id, user_id=user_id)
        self.session.add(item)
        self._apply_group_defaults(group, user)
        self.session.flush()
        self.add_admin_audit(actor_user_id, "group_add_member", "group", group_id, {"user_id": user_id})
        return item

    def list_group_members(self, group_id: str) -> list[UserRecord]:
        return list(self.session.scalars(select(UserRecord).join(GroupMembershipRecord, GroupMembershipRecord.user_id == UserRecord.user_id).where(GroupMembershipRecord.group_id == group_id).order_by(UserRecord.display_name)))

    def remove_group_member(self, actor_user_id: str, group_id: str, user_id: str) -> None:
        self.require_admin(actor_user_id)
        if self.group_membership_is_automatic(group_id):
            raise ValueError("自动 Group 的成员由邮箱策略维护，不能手动修改")
        item = self.session.scalar(select(GroupMembershipRecord).where(
            GroupMembershipRecord.group_id == group_id,
            GroupMembershipRecord.user_id == user_id,
        ))
        if item is None:
            raise FileNotFoundError("Group membership 不存在")
        self.session.delete(item)
        self.add_admin_audit(actor_user_id, "group_remove_member", "group", group_id, {"user_id": user_id})
        self.session.flush()

    def replace_group_members(
        self,
        actor_user_id: str,
        group_id: str,
        user_ids: Iterable[str],
    ) -> dict[str, list[str]]:
        """用一次事务同步自定义 Group 的完整成员集合，并记录成员差异。"""
        self.require_admin(actor_user_id)
        group = self.session.get(GroupRecord, group_id)
        if group is None:
            raise FileNotFoundError("Group 不存在")
        if self.group_membership_is_automatic(group_id):
            raise ValueError("自动 Group 的成员由邮箱策略维护，不能手动修改")
        target_ids = {str(user_id).strip() for user_id in user_ids if str(user_id).strip()}
        users = {user_id: self.get_user_any_status(user_id) for user_id in target_ids}
        existing_items = list(self.session.scalars(select(GroupMembershipRecord).where(
            GroupMembershipRecord.group_id == group_id
        )))
        existing_ids = {item.user_id for item in existing_items}
        added_ids = sorted(target_ids - existing_ids)
        removed_ids = sorted(existing_ids - target_ids)
        for item in existing_items:
            if item.user_id in removed_ids:
                self.session.delete(item)
        for user_id in added_ids:
            self.session.add(GroupMembershipRecord(
                membership_id=new_id("membership"), group_id=group_id, user_id=user_id
            ))
            self._apply_group_defaults(group, users[user_id])
        self.add_admin_audit(actor_user_id, "group_members_replace", "group", group_id, {
            "added_user_ids": added_ids,
            "removed_user_ids": removed_ids,
        })
        self.session.flush()
        return {"added_user_ids": added_ids, "removed_user_ids": removed_ids}

    def replace_user_custom_groups(
        self,
        actor_user_id: str,
        user_id: str,
        group_ids: Iterable[str],
    ) -> dict[str, list[str]]:
        """替换用户的自定义 Group，同时完整保留邮箱策略维护的自动归属。"""
        self.require_admin(actor_user_id)
        user = self.get_user_any_status(user_id)
        target_ids = {str(group_id).strip() for group_id in group_ids if str(group_id).strip()}
        groups: dict[str, GroupRecord] = {}
        for group_id in target_ids:
            group = self.session.get(GroupRecord, group_id)
            if group is None:
                raise FileNotFoundError(f"Group 不存在：{group_id}")
            if self.group_membership_is_automatic(group_id):
                raise ValueError("自动 Group 只能只读展示，不能通过成员编辑器修改")
            groups[group_id] = group
        existing_items = list(self.session.scalars(select(GroupMembershipRecord).where(
            GroupMembershipRecord.user_id == user_id
        )))
        existing_custom = {
            item.group_id: item
            for item in existing_items
            if not self.group_membership_is_automatic(item.group_id)
        }
        existing_ids = set(existing_custom)
        added_ids = sorted(target_ids - existing_ids)
        removed_ids = sorted(existing_ids - target_ids)
        for group_id in removed_ids:
            self.session.delete(existing_custom[group_id])
        for group_id in added_ids:
            self.session.add(GroupMembershipRecord(
                membership_id=new_id("membership"), group_id=group_id, user_id=user_id
            ))
            self._apply_group_defaults(groups[group_id], user)
        self.add_admin_audit(actor_user_id, "user_groups_replace", "user", user_id, {
            "added_group_ids": added_ids,
            "removed_group_ids": removed_ids,
        })
        self.session.flush()
        return {"added_group_ids": added_ids, "removed_group_ids": removed_ids}

    def update_group(self, actor_user_id: str, group_id: str, **changes: Any) -> GroupRecord:
        self.require_admin(actor_user_id)
        item = self.session.get(GroupRecord, group_id)
        if item is None:
            raise FileNotFoundError("Group 不存在")
        allowed = {
            "name", "description", "status", "default_monthly_cost_limit",
            "default_max_concurrent_runs", "default_model_strategy", "default_budget_preset",
        }
        for key, value in changes.items():
            if key in allowed and value is not None:
                setattr(item, key, value)
        item.updated_at = utc_now()
        # Group defaults are an administrator-controlled bulk policy.  Applying a
        # default immediately updates current members; later members receive the same
        # defaults in add_group_member().
        members = self.list_group_members(group_id)
        for user in members:
            if "default_monthly_cost_limit" in changes and changes.get("default_monthly_cost_limit") is not None:
                user.monthly_cost_limit = float(changes["default_monthly_cost_limit"])
            if "default_max_concurrent_runs" in changes and changes.get("default_max_concurrent_runs") is not None:
                user.max_concurrent_research_runs = max(1, int(changes["default_max_concurrent_runs"]))
            if changes.get("default_model_strategy"):
                user.default_model_strategy = str(changes["default_model_strategy"])
            if changes.get("default_budget_preset"):
                user.default_budget_preset = str(changes["default_budget_preset"])
            user.updated_at = utc_now()
        self.add_admin_audit(actor_user_id, "group_update", "group", group_id, {k: v for k, v in changes.items() if k in allowed})
        self.session.flush()
        return item

    def delete_group(self, actor_user_id: str, group_id: str) -> None:
        self.require_admin(actor_user_id)
        item = self.session.get(GroupRecord, group_id)
        if item is None:
            raise FileNotFoundError("Group 不存在")
        if item.system_key:
            raise ValueError("系统 Group 不能删除")
        self.add_admin_audit(actor_user_id, "group_delete", "group", group_id, {"name": item.name})
        self.session.delete(item)
        self.session.flush()

    def grant_credit(self, actor_user_id: str, user_id: str, amount: float, reason: str, group_id: str | None = None, run_id: str | None = None, source_key: str | None = None) -> CreditLedgerRecord:
        self.require_admin(actor_user_id)
        user = self.get_user_any_status(user_id)
        delta = self._credit(amount)
        user.credit_balance = self._credit(user.credit_balance) + delta
        item = CreditLedgerRecord(
            credit_event_id=new_id("credit"), user_id=user_id, group_id=group_id,
            subject_email=user.email, subject_storage_key=user.storage_key,
            amount=delta, reason=reason.strip() or "admin adjustment",
            created_by=actor_user_id, run_id=run_id, source_key=source_key,
        )
        self.session.add(item)
        self.session.flush()
        self.add_admin_audit(actor_user_id, "credit_grant", "user", user_id, {"amount": amount, "group_id": group_id, "reason": reason})
        return item

    def grant_group_credit(self, actor_user_id: str, group_id: str, amount: float, reason: str) -> int:
        members = self.list_group_members(group_id)
        for user in members:
            self.grant_credit(actor_user_id, user.user_id, amount, reason, group_id=group_id)
        return len(members)

    def update_user_admin(self, actor_user_id: str, user_id: str, *, role: str | None = None, is_developer: bool | None = None, status: str | None = None, monthly_cost_limit: float | None = None, storage_quota_bytes: int | None = None, max_concurrent_research_runs: int | None = None, default_model_strategy: str | None = None, default_budget_preset: str | None = None) -> UserRecord:
        self.require_admin(actor_user_id)
        user = self.session.get(UserRecord, user_id)
        if user is None:
            raise FileNotFoundError("用户不存在")
        if role is not None:
            if role not in {"user", "admin"}:
                raise ValueError("role 必须为 user/admin")
            user.role = role
        if is_developer is not None:
            user.is_developer = bool(is_developer)
        if status is not None:
            if status not in {"pending_verification", "active", "disabled"}:
                raise ValueError("status 必须为 pending_verification/active/disabled")
            if status == "active" and user.email_verified_at is None:
                status = "pending_verification"
            user.status = status
            if status == "disabled":
                self.revoke_all_auth_sessions(user_id)
        if monthly_cost_limit is not None:
            user.monthly_cost_limit = self._credit(monthly_cost_limit)
        if storage_quota_bytes is not None:
            if int(storage_quota_bytes) < 0:
                raise ValueError("storage_quota_bytes 不能为负")
            user.storage_quota_bytes = int(storage_quota_bytes)
        if max_concurrent_research_runs is not None:
            user.max_concurrent_research_runs = max(1, int(max_concurrent_research_runs))
        if default_model_strategy is not None:
            user.default_model_strategy = default_model_strategy.strip() or user.default_model_strategy
        if default_budget_preset is not None:
            user.default_budget_preset = default_budget_preset.strip() or user.default_budget_preset
        user.updated_at = utc_now()
        self.add_admin_audit(actor_user_id, "user_update", "user", user_id, {
            "role": role, "is_developer": is_developer, "status": status,
            "monthly_cost_limit": monthly_cost_limit, "storage_quota_bytes": storage_quota_bytes,
            "max_concurrent_research_runs": max_concurrent_research_runs,
            "default_model_strategy": default_model_strategy,
            "default_budget_preset": default_budget_preset,
        })
        self.session.flush()
        return user

    def precreate_pending_user(
        self,
        actor_user_id: str,
        email: str,
        *,
        display_name: str | None = None,
        is_developer: bool = False,
        storage_quota_bytes: int = 536_870_912,
    ) -> UserRecord:
        """管理员预创建尚未验证的邮箱账户，不创建登录 Session。"""
        self.require_admin(actor_user_id)
        normalized = email.strip().lower()
        if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("邮箱格式无效")
        existing = self.find_user_by_email(normalized)
        if existing is not None:
            return existing
        user = UserRecord(
            user_id=new_id("user"), storage_key=self.allocate_storage_key(),
            email=normalized, display_name=(display_name or normalized.split("@", 1)[0]).strip(),
            role="user", is_developer=bool(is_developer),
            status="pending_verification", credit_balance=self._credit(0),
            storage_quota_bytes=int(storage_quota_bytes), max_concurrent_research_runs=1,
        )
        self.session.add(user)
        self.session.flush()
        self.add_admin_audit(actor_user_id, "pending_user_create", "user", user.user_id, {"email": normalized})
        return user

    def ensure_bootstrap_user(self, email: str, credit: float, max_concurrent_runs: int = 10) -> UserRecord | None:
        """启动时预留 user_000001，但只有真实 OTP 才把账户激活。"""
        normalized = email.strip().lower()
        if not normalized:
            return None
        user = self.find_user_by_email(normalized)
        if user is None:
            occupied = self.session.scalar(select(UserRecord.user_id).where(
                UserRecord.storage_key == "user_000001"
            ))
            if occupied is not None:
                raise RuntimeError("user_000001 已被非 bootstrap 用户占用")
            user = UserRecord(
                user_id=new_id("user"), storage_key=self.allocate_storage_key("user_000001"),
                email=normalized, display_name=normalized.split("@", 1)[0],
                role="admin", is_developer=True, status="pending_verification",
                credit_balance=self._credit(credit), storage_quota_bytes=None,
                max_concurrent_research_runs=max(1, int(max_concurrent_runs)),
            )
            self.session.add(user); self.session.flush()
            user.storage_quota_bytes = None
            if self._credit(credit) != 0:
                self.session.add(CreditLedgerRecord(
                    credit_event_id=new_id("credit"), user_id=user.user_id,
                    subject_email=user.email, subject_storage_key=user.storage_key,
                    amount=self._credit(credit), reason="bootstrap admin credit",
                    source_key="bootstrap_admin_v1",
                ))
        else:
            user.storage_key = user.storage_key or "user_000001"
            user.role = "admin"; user.is_developer = True
            user.storage_quota_bytes = None
            user.max_concurrent_research_runs = max(1, int(max_concurrent_runs))
        self.ensure_identity_policies()
        self._apply_verified_email_policy(user, skip_welcome=True)
        self.session.flush()
        return user

    def sync_role_run_limits(self, *, admin_limit: int, user_limit: int) -> None:
        """启动时把固定身份档位同步到现有账户，不引入可变管理接口。"""
        admin_value = max(1, int(admin_limit))
        user_value = max(1, int(user_limit))
        for user in self.session.scalars(select(UserRecord)):
            user.max_concurrent_research_runs = admin_value if user.role == "admin" else user_value
        self.session.flush()

    def delete_empty_pending_user(self, actor_user_id: str, user_id: str) -> None:
        """硬删除无业务数据 pending 用户，同时保留已产生的 Credit/Admin 审计快照。"""
        self.require_admin(actor_user_id)
        user = self.get_user_any_status(user_id)
        if user.status != "pending_verification" or user.email_verified_at is not None:
            raise ValueError("只能删除尚未验证的 pending 用户")
        checks = (
            select(func.count()).select_from(ContractConversationRecord).where(ContractConversationRecord.owner_user_id == user_id),
            select(func.count()).select_from(FileAssetRecord).where(FileAssetRecord.owner_user_id == user_id),
            select(func.count()).select_from(ResearchRunRecord).where(ResearchRunRecord.owner_user_id == user_id),
            select(func.count()).select_from(UsageLedgerRecord).where(UsageLedgerRecord.owner_user_id == user_id),
        )
        if any(int(self.session.scalar(statement) or 0) for statement in checks):
            raise ValueError("pending 用户已有 Conversation/File/Run/Usage，不能删除")
        for entry in self.session.scalars(select(CreditLedgerRecord).where(CreditLedgerRecord.user_id == user_id)):
            entry.subject_email = entry.subject_email or user.email
            entry.subject_storage_key = entry.subject_storage_key or user.storage_key
            entry.user_id = None
        self.add_admin_audit(actor_user_id, "pending_user_delete", "user", user_id, {"email": user.email, "storage_key": user.storage_key})
        self.session.delete(user)
        self.session.flush()

    def list_user_groups(self, user_id: str) -> list[GroupRecord]:
        self.get_user_any_status(user_id)
        return list(self.session.scalars(
            select(GroupRecord)
            .join(GroupMembershipRecord, GroupMembershipRecord.group_id == GroupRecord.group_id)
            .where(GroupMembershipRecord.user_id == user_id)
            .order_by(GroupRecord.name)
        ))

    def list_credit_ledger(self, user_id: str | None = None) -> list[CreditLedgerRecord]:
        statement = select(CreditLedgerRecord)
        if user_id is not None:
            statement = statement.where(CreditLedgerRecord.user_id == user_id)
        return list(self.session.scalars(statement.order_by(CreditLedgerRecord.created_at.desc())))

    def list_admin_audit(self, actor_user_id: str, limit: int = 200) -> list[AdminAuditRecord]:
        self.require_admin(actor_user_id)
        return list(self.session.scalars(select(AdminAuditRecord).order_by(AdminAuditRecord.created_at.desc()).limit(limit)))

    # ---------- projects / folders ----------
    def create_project(self, user_id: str, name: str, description: str = "") -> ProjectRecord:
        self.get_user(user_id)
        next_order = int(
            self.session.scalar(
                select(func.coalesce(func.max(ProjectRecord.sort_order), -1)).where(
                    ProjectRecord.owner_user_id == user_id
                )
            )
            or 0
        ) + 1
        item = ProjectRecord(
            project_id=new_id("project"),
            owner_user_id=user_id,
            name=name.strip() or "未命名 Project",
            description=description.strip(),
            sort_order=next_order,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def list_projects(self, user_id: str) -> list[ProjectRecord]:
        return list(
            self.session.scalars(
                select(ProjectRecord)
                .where(ProjectRecord.owner_user_id == user_id)
                .order_by(ProjectRecord.sort_order, ProjectRecord.created_at, ProjectRecord.project_id)
            )
        )

    def get_project(self, user_id: str, project_id: str) -> ProjectRecord:
        item = self.session.get(ProjectRecord, project_id)
        if item is None or item.owner_user_id != user_id:
            raise OwnershipError("Project 不存在或不属于当前用户")
        return item

    def rename_project(self, user_id: str, project_id: str, name: str) -> ProjectRecord:
        item = self.get_project(user_id, project_id)
        item.name = name.strip() or item.name
        item.updated_at = utc_now()
        self.session.flush()
        return item

    def delete_project(self, user_id: str, project_id: str) -> None:
        """永久删除整理型 Project 及其全部研究内容，但保留独立 Usage Ledger。"""
        self.get_project(user_id, project_id)
        conversation_ids = list(
            self.session.scalars(
                select(ContractConversationRecord.conversation_id).where(
                    ContractConversationRecord.project_id == project_id,
                    ContractConversationRecord.owner_user_id == user_id,
                )
            )
        )
        # ResearchRun 使用 RESTRICT 指向冻结 ContractVersion，因此先显式删除 Run，
        # 再删除 Conversation/ContractVersion，最后删除纯整理型 Project。
        for conversation_id in conversation_ids:
            self._delete_conversation_tree_by_id(conversation_id)
        self.session.execute(
            delete(ProjectRecord).where(
                ProjectRecord.project_id == project_id,
                ProjectRecord.owner_user_id == user_id,
            ),
            execution_options={"synchronize_session": False},
        )
        self.session.flush()

    def deletion_target_is_active(self, user_id: str, target_type: str, target_id: str) -> bool:
        """判断 Project/Conversation 是否仍有生成或研究任务，供删除入口选择同步或排队。"""
        conversation_ids = self._deletion_conversation_ids(user_id, target_type, target_id)
        if not conversation_ids:
            return False
        generation = self.session.scalar(select(func.count()).select_from(ContractGenerationAttemptRecord).where(
            ContractGenerationAttemptRecord.conversation_id.in_(conversation_ids),
            ContractGenerationAttemptRecord.status.in_(["queued", "running", "repairing"]),
        ))
        runs = self.session.scalar(select(func.count()).select_from(ResearchRunRecord).where(
            ResearchRunRecord.conversation_id.in_(conversation_ids),
            ResearchRunRecord.status.in_(["created", "queued", "resuming", "running", "draining", "paused"]),
        ))
        return bool(generation or runs)

    def request_deletion(
        self, user_id: str, target_type: str, target_id: str, *, terminate_active: bool
    ) -> tuple[bool, DeletionJobRecord | None]:
        """空闲目标同步删除；活跃目标幂等创建持久化终止删除任务。"""
        if target_type == "project":
            target = self.get_project(user_id, target_id)
            target_name = target.name
        elif target_type == "conversation":
            target = self.get_conversation(user_id, target_id)
            target_name = target.display_name
        else:
            raise ValueError("未知删除目标类型")
        existing = self.session.scalar(select(DeletionJobRecord).where(
            DeletionJobRecord.owner_user_id == user_id,
            DeletionJobRecord.target_type == target_type,
            DeletionJobRecord.target_id == target_id,
            DeletionJobRecord.status.in_(["queued", "draining"]),
        ).order_by(DeletionJobRecord.created_at.desc()).limit(1))
        if existing is not None:
            return False, existing
        if self.deletion_target_is_active(user_id, target_type, target_id):
            if not terminate_active:
                raise ValueError("目标仍有活跃任务；需要明确选择终止并删除")
            job = DeletionJobRecord(
                deletion_job_id=new_id("delete"), owner_user_id=user_id,
                target_type=target_type, target_id=target_id, target_name=target_name,
                status="queued",
            )
            self.session.add(job)
            self.session.flush()
            return False, job
        if target_type == "project":
            self.delete_project(user_id, target_id)
        else:
            self.delete_conversation(user_id, target_id)
        return True, None

    def _deletion_conversation_ids(self, user_id: str, target_type: str, target_id: str) -> list[str]:
        if target_type == "conversation":
            item = self.session.get(ContractConversationRecord, target_id)
            return [target_id] if item is not None and item.owner_user_id == user_id else []
        if target_type == "project":
            return list(self.session.scalars(select(ContractConversationRecord.conversation_id).where(
                ContractConversationRecord.owner_user_id == user_id,
                ContractConversationRecord.project_id == target_id,
            )))
        return []

    def deletion_conversation_ids(self, job: DeletionJobRecord) -> list[str]:
        return self._deletion_conversation_ids(job.owner_user_id, job.target_type, job.target_id)

    def get_deletion_job(self, user_id: str, deletion_job_id: str) -> DeletionJobRecord:
        item = self.session.get(DeletionJobRecord, deletion_job_id)
        if item is None or item.owner_user_id != user_id:
            raise OwnershipError("Deletion Job 不存在或不属于当前用户")
        return item

    def list_active_deletion_jobs(self, user_id: str) -> list[DeletionJobRecord]:
        return list(self.session.scalars(select(DeletionJobRecord).where(
            DeletionJobRecord.owner_user_id == user_id,
            DeletionJobRecord.status.in_(["queued", "draining"]),
        ).order_by(DeletionJobRecord.created_at)))

    def release_stale_deletion_claims(self) -> int:
        items = list(self.session.scalars(select(DeletionJobRecord).where(
            DeletionJobRecord.status == "draining",
            DeletionJobRecord.claimed_by_runner.is_not(None),
        )))
        for item in items:
            item.status = "queued"; item.claimed_by_runner = None; item.claimed_at = None; item.updated_at = utc_now()
        self.session.flush()
        return len(items)

    def claim_next_deletion_job(self, runner_id: str) -> DeletionJobRecord | None:
        statement = select(DeletionJobRecord).where(
            DeletionJobRecord.status == "queued",
            DeletionJobRecord.claimed_by_runner.is_(None),
        ).order_by(DeletionJobRecord.created_at).limit(1)
        if self.session.bind is not None and self.session.bind.dialect.name != "sqlite":
            statement = statement.with_for_update(skip_locked=True)
        item = self.session.scalar(statement)
        if item is None:
            return None
        item.status = "draining"; item.claimed_by_runner = runner_id; item.claimed_at = utc_now(); item.updated_at = utc_now(); item.error = None
        self.session.flush()
        return item

    def finish_deletion_job(self, deletion_job_id: str, *, error: str | None = None) -> DeletionJobRecord:
        item = self.session.get(DeletionJobRecord, deletion_job_id)
        if item is None:
            raise FileNotFoundError("Deletion Job 不存在")
        item.status = "failed" if error else "completed"
        item.error = error; item.claimed_by_runner = None; item.claimed_at = None; item.updated_at = utc_now()
        if not error:
            item.completed_at = utc_now()
        self.session.flush()
        return item

    # ---------- conversations ----------
    def create_conversation(
        self,
        user_id: str,
        research_request: str = "",
        *,
        project_id: str | None = None,
        display_name: str | None = None,
        source_file_ids: list[str] | None = None,
        research_problem: str = "",
        background_and_prior_progress: str = "",
        method_preferences_and_restrictions: str = "",
        valuable_partial_outcomes: str = "",
        possible_ideas_and_rough_approaches: str = "",
        contractor_profile: str = "high",
        contract_language: str | None = None,
        execution_policy: dict[str, Any] | None = None,
        status: str = "request_draft",
    ) -> ContractConversationRecord:
        user = self.get_user(user_id)
        if project_id is not None:
            self.get_project(user_id, project_id)
        project_condition = (
            ContractConversationRecord.project_id.is_(None)
            if project_id is None
            else ContractConversationRecord.project_id == project_id
        )
        next_order = int(
            self.session.scalar(
                select(func.coalesce(func.max(ContractConversationRecord.sort_order), -1)).where(
                    ContractConversationRecord.owner_user_id == user_id,
                    project_condition,
                )
            )
            or 0
        ) + 1
        seed = research_problem.strip() or research_request.strip()
        title = display_name or self._request_title(seed)
        normalized_files = list(dict.fromkeys(source_file_ids or []))
        for file_id in normalized_files:
            self.get_file_asset(user_id, file_id)
        item = ContractConversationRecord(
            conversation_id=new_id("chat"),
            owner_user_id=user_id,
            project_id=project_id,
            sort_order=next_order,
            display_name=title,
            research_request=research_request.strip(),
            research_problem=research_problem.strip(),
            background_and_prior_progress=background_and_prior_progress.strip(),
            method_preferences_and_restrictions=method_preferences_and_restrictions.strip(),
            valuable_partial_outcomes=valuable_partial_outcomes.strip(),
            possible_ideas_and_rough_approaches=possible_ideas_and_rough_approaches.strip(),
            contractor_profile=contractor_profile.strip() or "high",
            contract_language=normalize_language(
                contract_language, default=user.default_contract_language
            ),
            request_draft_revision=1,
            source_file_ids=normalized_files,
            execution_policy=dict(execution_policy or {}),
            status=status,
        )
        self.session.add(item)
        self.session.flush()
        # Original Request is provenance, not visible Contract Chat.
        if research_request.strip():
            self.append_message(item.conversation_id, "user", "research_request_audit", research_request)
        return item

    def update_conversation_execution_policy(
        self,
        user_id: str,
        conversation_id: str,
        execution_policy: dict[str, Any],
    ) -> ContractConversationRecord:
        """保存对话级执行档位；它独立于 Contract 文字版本和 Working Draft。"""
        item = self.get_conversation(user_id, conversation_id)
        item.execution_policy = dict(execution_policy)
        item.updated_at = utc_now()
        self.session.flush()
        return item

    def update_request_draft(
        self,
        user_id: str,
        conversation_id: str,
        *,
        expected_revision: int,
        research_request: str,
        research_problem: str,
        background_and_prior_progress: str,
        method_preferences_and_restrictions: str,
        valuable_partial_outcomes: str,
        possible_ideas_and_rough_approaches: str,
        contractor_profile: str,
        project_id: str | None,
        source_file_ids: list[str],
        display_name: str | None = None,
        contract_language: str | None = None,
    ) -> ContractConversationRecord:
        item = self.get_conversation(user_id, conversation_id)
        if item.status != "request_draft" or item.latest_contract_version or item.locked_at:
            raise ValueError("该 Research Request 已进入生成或执行阶段，不能继续修改")
        if int(item.request_draft_revision) != int(expected_revision):
            raise ValueError("DraftRevisionConflict")
        if project_id is not None:
            self.get_project(user_id, project_id)
        normalized_files = list(dict.fromkeys(source_file_ids))
        for file_id in normalized_files:
            self.get_file_asset(user_id, file_id)
        normalized_request = research_request.strip()
        normalized_problem = research_problem.strip()
        normalized_background = background_and_prior_progress.strip()
        normalized_methods = method_preferences_and_restrictions.strip()
        normalized_partial = valuable_partial_outcomes.strip()
        normalized_ideas = possible_ideas_and_rough_approaches.strip()
        normalized_profile = contractor_profile.strip() or item.contractor_profile
        normalized_language = normalize_language(
            contract_language, default=item.contract_language
        )
        normalized_display_name = item.display_name
        if display_name is not None and display_name.strip():
            normalized_display_name = display_name.strip()
        elif not item.display_name.strip():
            normalized_display_name = self._request_title(normalized_problem or normalized_request)
        unchanged = (
            item.research_request == normalized_request
            and item.research_problem == normalized_problem
            and item.background_and_prior_progress == normalized_background
            and item.method_preferences_and_restrictions == normalized_methods
            and item.valuable_partial_outcomes == normalized_partial
            and item.possible_ideas_and_rough_approaches == normalized_ideas
            and item.contractor_profile == normalized_profile
            and item.contract_language == normalized_language
            and item.project_id == project_id
            and list(item.source_file_ids or []) == normalized_files
            and item.display_name == normalized_display_name
        )
        if unchanged:
            return item
        source_project_id = item.project_id
        if source_project_id != project_id:
            item.project_id = project_id
            target_rows = self._ordered_conversation_group(user_id, project_id)
            item.sort_order = len(
                [row for row in target_rows if row.conversation_id != conversation_id]
            )
            self._normalize_conversation_group(
                user_id, source_project_id, exclude_id=conversation_id
            )
        item.research_request = normalized_request
        item.research_problem = normalized_problem
        item.background_and_prior_progress = normalized_background
        item.method_preferences_and_restrictions = normalized_methods
        item.valuable_partial_outcomes = normalized_partial
        item.possible_ideas_and_rough_approaches = normalized_ideas
        item.contractor_profile = normalized_profile
        item.contract_language = normalized_language
        item.source_file_ids = normalized_files
        item.display_name = normalized_display_name
        item.request_draft_revision += 1
        item.updated_at = utc_now()
        self.session.flush()
        return item

    def list_conversations(
        self, user_id: str, *, project_id: str | None | object = ...
    ) -> list[ContractConversationRecord]:
        statement = select(ContractConversationRecord).options(
            selectinload(ContractConversationRecord.research_run)
        ).where(ContractConversationRecord.owner_user_id == user_id)
        if project_id is not ...:
            statement = statement.where(ContractConversationRecord.project_id == project_id)
        return list(
            self.session.scalars(
                statement.order_by(
                    ContractConversationRecord.sort_order,
                    ContractConversationRecord.created_at,
                    ContractConversationRecord.conversation_id,
                )
            )
        )

    def get_conversation(self, user_id: str, conversation_id: str) -> ContractConversationRecord:
        item = self.session.get(ContractConversationRecord, conversation_id)
        if item is None or item.owner_user_id != user_id:
            raise OwnershipError("Contract Conversation 不存在或不属于当前用户")
        return item

    def rename_conversation(
        self, user_id: str, conversation_id: str, name: str
    ) -> ContractConversationRecord:
        item = self.get_conversation(user_id, conversation_id)
        item.display_name = name.strip() or item.display_name
        item.updated_at = utc_now()
        self.session.flush()
        return item

    def move_conversation(
        self, user_id: str, conversation_id: str, project_id: str | None
    ) -> ContractConversationRecord:
        item = self.get_conversation(user_id, conversation_id)
        if project_id is not None:
            self.get_project(user_id, project_id)
        source_project_id = item.project_id
        if source_project_id == project_id:
            return item
        item.project_id = project_id
        target_rows = self._ordered_conversation_group(user_id, project_id)
        item.sort_order = len([row for row in target_rows if row.conversation_id != conversation_id])
        item.updated_at = utc_now()
        self._normalize_conversation_group(user_id, source_project_id, exclude_id=conversation_id)
        self.session.flush()
        return item

    def reorder_project(self, user_id: str, project_id: str, target_index: int) -> list[str]:
        """把一个 Project 移到用户 Project 列表中的指定位置并持久化顺序。"""
        item = self.get_project(user_id, project_id)
        rows = [row for row in self.list_projects(user_id) if row.project_id != project_id]
        rows.insert(max(0, min(int(target_index), len(rows))), item)
        for index, row in enumerate(rows):
            row.sort_order = index
        self.session.flush()
        return [row.project_id for row in rows]

    def reorder_conversation(
        self,
        user_id: str,
        conversation_id: str,
        *,
        target_project_id: str | None,
        target_index: int,
    ) -> list[str]:
        """移动或归档 Conversation，并在目标分组内持久化精确顺序。"""
        item = self.get_conversation(user_id, conversation_id)
        if target_project_id is not None:
            self.get_project(user_id, target_project_id)
        source_project_id = item.project_id
        target_rows = [
            row
            for row in self._ordered_conversation_group(user_id, target_project_id)
            if row.conversation_id != conversation_id
        ]
        item.project_id = target_project_id
        target_rows.insert(max(0, min(int(target_index), len(target_rows))), item)
        for index, row in enumerate(target_rows):
            row.sort_order = index
        if source_project_id != target_project_id:
            self._normalize_conversation_group(
                user_id, source_project_id, exclude_id=conversation_id
            )
        item.updated_at = utc_now()
        self.session.flush()
        return [row.conversation_id for row in target_rows]

    def _ordered_conversation_group(
        self, user_id: str, project_id: str | None
    ) -> list[ContractConversationRecord]:
        condition = (
            ContractConversationRecord.project_id.is_(None)
            if project_id is None
            else ContractConversationRecord.project_id == project_id
        )
        return list(
            self.session.scalars(
                select(ContractConversationRecord)
                .where(
                    ContractConversationRecord.owner_user_id == user_id,
                    condition,
                )
                .order_by(
                    ContractConversationRecord.sort_order,
                    ContractConversationRecord.created_at,
                    ContractConversationRecord.conversation_id,
                )
            )
        )

    def _normalize_conversation_group(
        self,
        user_id: str,
        project_id: str | None,
        *,
        exclude_id: str | None = None,
    ) -> None:
        rows = [
            row for row in self._ordered_conversation_group(user_id, project_id)
            if row.conversation_id != exclude_id
        ]
        for index, row in enumerate(rows):
            row.sort_order = index

    def delete_conversation(self, user_id: str, conversation_id: str) -> None:
        """永久删除一个 Contract Conversation 及其 Run/Artifacts。"""
        self.get_conversation(user_id, conversation_id)
        self._delete_conversation_tree_by_id(conversation_id)

    def delete_contract_version(
        self, user_id: str, conversation_id: str, version: int
    ) -> dict[str, Any]:
        """删除一个未进入执行阶段的 immutable Contract Version 及其版本级 Chat。"""
        conversation = self.get_conversation(user_id, conversation_id)
        if conversation.locked_at or conversation.status in {
            "execution_locked", "running", "completed"
        }:
            raise ValueError("执行开始后不能删除 Contract Version")
        active_generation = self.session.scalar(
            select(ContractGenerationAttemptRecord.generation_attempt_id).where(
                ContractGenerationAttemptRecord.conversation_id == conversation_id,
                ContractGenerationAttemptRecord.status.in_(["queued", "running", "repairing"]),
            ).limit(1)
        )
        if active_generation is not None:
            raise ValueError("Contract 正在生成，不能删除其版本")
        item = self.get_contract_version(user_id, conversation_id, version)
        if (
            conversation.working_draft is not None
            and int(conversation.working_draft.base_version) == int(version)
        ):
            raise ValueError("Working Draft 正基于该版本；请先保存或放弃草稿")
        referenced_run = self.session.scalar(
            select(ResearchRunRecord.run_id).where(
                ResearchRunRecord.contract_version_id == item.contract_version_id
            ).limit(1)
        )
        if referenced_run is not None:
            raise ValueError("该 Contract Version 已被 Research Run 锁定，不能删除")

        self.session.execute(
            delete(AgentThreadRecord).where(
                AgentThreadRecord.scope_type == "contract_version",
                AgentThreadRecord.scope_id == item.contract_version_id,
            ),
            execution_options={"synchronize_session": False},
        )
        self.session.execute(
            delete(RuntimeEventRecord).where(
                RuntimeEventRecord.scope_type == "contract_chat",
                RuntimeEventRecord.scope_id == item.contract_version_id,
            ),
            execution_options={"synchronize_session": False},
        )
        markdown_path = self.artifacts.resolve(item.markdown_path)
        if markdown_path.exists():
            markdown_path.unlink()
        deleted_version_id = item.contract_version_id
        self.session.delete(item)
        self.session.flush()

        latest = int(
            self.session.scalar(
                select(func.coalesce(func.max(ContractVersionRecord.version), 0)).where(
                    ContractVersionRecord.conversation_id == conversation_id
                )
            )
            or 0
        )
        conversation.latest_contract_version = latest
        if latest == 0:
            conversation.status = "request_draft"
            if not conversation.research_problem.strip():
                conversation.research_problem = conversation.research_request.strip()
        else:
            conversation.status = "ready"
        conversation.updated_at = utc_now()
        self.session.flush()
        self.append_event(
            "conversation",
            conversation_id,
            "contract_version_deleted",
            f"Contract v{version} 已永久删除",
            {
                "deleted_version": version,
                "deleted_contract_version_id": deleted_version_id,
                "latest_contract_version": latest,
            },
        )
        return {
            "deleted": True,
            "deleted_version": int(version),
            "latest_contract_version": latest,
            "status": conversation.status,
        }

    def _delete_conversation_tree_by_id(self, conversation_id: str) -> None:
        """按外键安全顺序清理 Conversation 内容；Usage Ledger 不参与级联。"""
        self._delete_conversation_artifacts(conversation_id)
        self.session.execute(
            delete(ResearchRunRecord).where(
                ResearchRunRecord.conversation_id == conversation_id
            ),
            execution_options={"synchronize_session": False},
        )
        self.session.flush()
        self.session.execute(
            delete(ContractConversationRecord).where(
                ContractConversationRecord.conversation_id == conversation_id
            ),
            execution_options={"synchronize_session": False},
        )
        self.session.flush()

    def set_conversation_sources(
        self, user_id: str, conversation_id: str, file_ids: list[str]
    ) -> ContractConversationRecord:
        item = self.get_conversation(user_id, conversation_id)
        if item.status in {"execution_locked", "running", "completed"}:
            raise ValueError("执行开始后不能补充或替换 Source Materials")
        normalized = list(dict.fromkeys(file_ids))
        for file_id in normalized:
            self.get_file_asset(user_id, file_id)
        item.source_file_ids = normalized
        item.updated_at = utc_now()
        if item.working_draft is not None:
            item.working_draft.source_manifest = self.source_manifest(user_id, normalized)
            item.working_draft.updated_at = utc_now()
        self.session.flush()
        return item

    def _delete_conversation_artifacts(self, conversation_id: str) -> None:
        conversation = self.session.get(ContractConversationRecord, conversation_id)
        if conversation is None:
            return
        for version in conversation.versions:
            path = self.artifacts.resolve(version.markdown_path)
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        if conversation.research_run:
            run_root = self.artifacts.resolve(conversation.research_run.run_root)
            if run_root.exists() and run_root.is_relative_to(self.artifacts.data_root):
                import shutil

                shutil.rmtree(run_root)
        owner = self.session.get(UserRecord, conversation.owner_user_id)
        if owner is not None and owner.storage_key:
            conversation_root = self.artifacts.conversation_root(
                owner.storage_key, conversation_id
            )
            if conversation_root.is_dir():
                import shutil

                shutil.rmtree(conversation_root)

    @staticmethod
    def _request_title(text: str) -> str:
        """从首个 Markdown 标题或正文段落生成默认对话名，显式名称仍由调用方优先。"""
        lines = [line.strip() for line in text.strip().splitlines()]
        heading = next((line.lstrip("#").strip() for line in lines if line.startswith("#") and line.lstrip("#").strip()), "")
        first_text = next((line for line in lines if line and not line.startswith("#")), "")
        compact = " ".join((heading or first_text).split())
        return compact[:42] + ("…" if len(compact) > 42 else "") or "新 Research Contract"

    def append_message(
        self, conversation_id: str, role: str, kind: str, content: str
    ) -> ChatMessageRecord:
        initial_value = int(self.session.scalar(
            select(func.coalesce(func.max(ChatMessageRecord.sequence), 0)).where(
                ChatMessageRecord.conversation_id == conversation_id
            )
        ) or 0) + 1
        sequence = self._next_scoped_sequence(
            "conversation_message", conversation_id, initial_value=initial_value
        )
        message = ChatMessageRecord(
            message_id=new_id("message"),
            conversation_id=conversation_id,
            sequence=sequence,
            role=role,
            kind=kind,
            content=content.rstrip(),
        )
        self.session.add(message)
        self.session.flush()
        return message

    # ---------- visible Contract Chat ----------
    def append_contract_chat_message(
        self,
        user_id: str,
        conversation_id: str,
        *,
        contract_version_id: str,
        role: str,
        content: str,
        draft_revision_before: int | None = None,
        draft_revision_after: int | None = None,
    ) -> ContractChatMessageRecord:
        self.get_conversation(user_id, conversation_id)
        version = self.session.get(ContractVersionRecord, contract_version_id)
        if version is None or version.conversation_id != conversation_id:
            raise OwnershipError("Contract Chat 版本不存在或不属于该 Conversation")
        initial_value = int(self.session.scalar(
            select(func.coalesce(func.max(ContractChatMessageRecord.sequence), 0)).where(
                ContractChatMessageRecord.conversation_id == conversation_id
            )
        ) or 0) + 1
        sequence = self._next_scoped_sequence(
            "contract_chat_message", conversation_id, initial_value=initial_value
        )
        item = ContractChatMessageRecord(
            chat_message_id=new_id("contract_chat"),
            conversation_id=conversation_id,
            contract_version_id=contract_version_id,
            sequence=sequence,
            role=role,
            content=content.rstrip(),
            draft_revision_before=draft_revision_before,
            draft_revision_after=draft_revision_after,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def list_contract_chat_messages(
        self,
        user_id: str,
        conversation_id: str,
        contract_version_id: str | None = None,
    ) -> list[ContractChatMessageRecord]:
        self.get_conversation(user_id, conversation_id)
        conditions = [ContractChatMessageRecord.conversation_id == conversation_id]
        if contract_version_id is not None:
            version = self.session.get(ContractVersionRecord, contract_version_id)
            if version is None or version.conversation_id != conversation_id:
                raise OwnershipError("Contract Chat 版本不存在或不属于该 Conversation")
            conditions.append(ContractChatMessageRecord.contract_version_id == contract_version_id)
        return list(
            self.session.scalars(
                select(ContractChatMessageRecord)
                .where(*conditions)
                .order_by(ContractChatMessageRecord.sequence)
            )
        )

    # ---------- Contract generation queue ----------
    def find_generation_attempt_by_idempotency(
        self, user_id: str, idempotency_key: str
    ) -> ContractGenerationAttemptRecord | None:
        item = self.session.scalar(
            select(ContractGenerationAttemptRecord).where(
                ContractGenerationAttemptRecord.idempotency_key == idempotency_key
            )
        )
        if item is None:
            return None
        if item.owner_user_id != user_id:
            raise OwnershipError("幂等键不属于当前用户")
        return item

    def create_generation_attempt(
        self,
        user_id: str,
        conversation_id: str,
        *,
        kind: str,
        contractor_profile: str,
        request_snapshot_path: Path,
        source_manifest: list[dict[str, Any]],
        base_contract_version_id: str | None = None,
        base_draft_revision: int | None = None,
        idempotency_key: str | None = None,
        expected_credit: float = 0.0,
    ) -> ContractGenerationAttemptRecord:
        conversation = self.get_conversation(user_id, conversation_id)
        active = self.session.scalar(
            select(ContractGenerationAttemptRecord)
            .where(
                ContractGenerationAttemptRecord.conversation_id == conversation_id,
                ContractGenerationAttemptRecord.status.in_(["queued", "running", "repairing"]),
            )
            .limit(1)
        )
        if active is not None:
            if idempotency_key and active.idempotency_key == idempotency_key:
                return active
            raise ValueError("该 Contract Conversation 已有生成任务正在运行")
        if idempotency_key:
            existing = self.session.scalar(
                select(ContractGenerationAttemptRecord).where(
                    ContractGenerationAttemptRecord.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.owner_user_id != user_id:
                    raise OwnershipError("幂等键不属于当前用户")
                return existing
        attempt_number = int(
            self.session.scalar(
                select(func.coalesce(func.max(ContractGenerationAttemptRecord.attempt_number), 0)).where(
                    ContractGenerationAttemptRecord.conversation_id == conversation_id
                )
            )
            or 0
        ) + 1
        item = ContractGenerationAttemptRecord(
            generation_attempt_id=new_id("contractgen"),
            conversation_id=conversation_id,
            owner_user_id=user_id,
            attempt_number=attempt_number,
            kind=kind,
            status="queued",
            contractor_profile=contractor_profile,
            request_snapshot_path=self.artifacts.reference(request_snapshot_path),
            source_manifest=source_manifest,
            base_contract_version_id=base_contract_version_id,
            base_draft_revision=base_draft_revision,
            idempotency_key=idempotency_key,
            expected_credit=self._credit(expected_credit),
            reserved_credit=self._credit(0),
            precharged_credit=self._credit(expected_credit),
        )
        self.session.add(item)
        self.session.flush()
        self._apply_credit_delta(
            user_id,
            -self._credit(expected_credit),
            reason=f"Contract generation precharge: {item.generation_attempt_id}",
            source_key=f"generation_precharge:{item.generation_attempt_id}",
        )
        self.ensure_generation_stages(item.generation_attempt_id)
        conversation.status = "contract_generating"
        conversation.updated_at = utc_now()
        self.session.flush()
        self.append_event(
            "contract_generation",
            item.generation_attempt_id,
            "contract_generation_queued",
            "Research Contract generation 已进入队列",
            {"conversation_id": conversation_id, "kind": kind},
        )
        return item

    @staticmethod
    def generation_stage_keys() -> tuple[str, ...]:
        return (
            "contractor_stage1",
            "contractor_stage2",
            "auditor_fidelity",
            "auditor_completeness",
            "auditor_map",
            "integrator",
        )

    def ensure_generation_stages(
        self, generation_attempt_id: str
    ) -> list[ContractGenerationStageRecord]:
        """为一次 Contract generation 建立固定六阶段 checkpoint。"""
        existing = {
            item.stage_key: item
            for item in self.session.scalars(
                select(ContractGenerationStageRecord).where(
                    ContractGenerationStageRecord.generation_attempt_id
                    == generation_attempt_id
                )
            )
        }
        for stage_key in self.generation_stage_keys():
            if stage_key not in existing:
                item = ContractGenerationStageRecord(
                    generation_stage_id=new_id("genstage"),
                    generation_attempt_id=generation_attempt_id,
                    stage_key=stage_key,
                    status="waiting",
                )
                self.session.add(item)
                existing[stage_key] = item
        self.session.flush()
        return [existing[key] for key in self.generation_stage_keys()]

    def list_generation_stages(
        self, generation_attempt_id: str
    ) -> list[ContractGenerationStageRecord]:
        return list(
            self.session.scalars(
                select(ContractGenerationStageRecord)
                .where(
                    ContractGenerationStageRecord.generation_attempt_id
                    == generation_attempt_id
                )
                .order_by(ContractGenerationStageRecord.generation_stage_id)
            )
        )

    def get_generation_stage(
        self, generation_attempt_id: str, stage_key: str
    ) -> ContractGenerationStageRecord:
        item = self.session.scalar(
            select(ContractGenerationStageRecord).where(
                ContractGenerationStageRecord.generation_attempt_id
                == generation_attempt_id,
                ContractGenerationStageRecord.stage_key == stage_key,
            )
        )
        if item is None:
            self.ensure_generation_stages(generation_attempt_id)
            item = self.session.scalar(
                select(ContractGenerationStageRecord).where(
                    ContractGenerationStageRecord.generation_attempt_id
                    == generation_attempt_id,
                    ContractGenerationStageRecord.stage_key == stage_key,
                )
            )
        if item is None:
            raise FileNotFoundError(f"Contract generation stage `{stage_key}` 不存在")
        return item

    def begin_generation_stage(
        self,
        generation_attempt_id: str,
        stage_key: str,
        *,
        repair_number: int = 0,
        provider_thread_id: str | None = None,
    ) -> ContractGenerationStageRecord:
        item = self.get_generation_stage(generation_attempt_id, stage_key)
        item.status = "running"
        item.repair_number = repair_number
        item.error = None
        item.started_at = item.started_at or utc_now()
        if provider_thread_id:
            item.provider_thread_id = provider_thread_id
        item.updated_at = utc_now()
        self.session.flush()
        return item

    def bind_generation_stage_thread(
        self, generation_attempt_id: str, stage_key: str, provider_thread_id: str
    ) -> ContractGenerationStageRecord:
        item = self.get_generation_stage(generation_attempt_id, stage_key)
        item.provider_thread_id = provider_thread_id
        item.updated_at = utc_now()
        self.session.flush()
        return item

    def complete_generation_stage(
        self,
        generation_attempt_id: str,
        stage_key: str,
        *,
        provider_thread_id: str | None,
        artifact_path: Path,
        artifact_sha256: str,
        repair_number: int,
        model: str,
        reasoning_effort: str,
    ) -> ContractGenerationStageRecord:
        item = self.get_generation_stage(generation_attempt_id, stage_key)
        item.status = "completed"
        item.provider_thread_id = provider_thread_id or item.provider_thread_id
        item.artifact_path = self.artifacts.reference(artifact_path)
        item.artifact_sha256 = artifact_sha256
        item.repair_number = repair_number
        item.model = model
        item.reasoning_effort = reasoning_effort
        item.error = None
        item.completed_at = utc_now()
        item.updated_at = utc_now()
        self.session.flush()
        return item

    def interrupt_running_generation_stages(
        self, generation_attempt_id: str, error: str
    ) -> None:
        for item in self.list_generation_stages(generation_attempt_id):
            if item.status == "running":
                item.status = "interrupted"
                item.error = error
                item.updated_at = utc_now()
        self.session.flush()

    def get_generation_attempt(
        self, user_id: str, generation_attempt_id: str
    ) -> ContractGenerationAttemptRecord:
        item = self.session.get(ContractGenerationAttemptRecord, generation_attempt_id)
        if item is None or item.owner_user_id != user_id:
            raise OwnershipError("Contract generation attempt 不存在或不属于当前用户")
        return item

    def latest_generation_attempt(
        self, user_id: str, conversation_id: str
    ) -> ContractGenerationAttemptRecord | None:
        self.get_conversation(user_id, conversation_id)
        return self.session.scalar(
            select(ContractGenerationAttemptRecord)
            .where(ContractGenerationAttemptRecord.conversation_id == conversation_id)
            .order_by(ContractGenerationAttemptRecord.attempt_number.desc())
            .limit(1)
        )

    def list_generation_attempts(
        self, user_id: str, conversation_id: str
    ) -> list[ContractGenerationAttemptRecord]:
        self.get_conversation(user_id, conversation_id)
        return list(
            self.session.scalars(
                select(ContractGenerationAttemptRecord)
                .where(ContractGenerationAttemptRecord.conversation_id == conversation_id)
                .order_by(ContractGenerationAttemptRecord.attempt_number)
            )
        )

    def release_stale_generation_claims(self) -> int:
        items = list(
            self.session.scalars(
                select(ContractGenerationAttemptRecord).where(
                    ContractGenerationAttemptRecord.status.in_(["running", "repairing"]),
                    ContractGenerationAttemptRecord.claimed_by_runner.is_not(None),
                )
            )
        )
        for item in items:
            item.status = "interrupted"
            item.claimed_by_runner = None
            item.claimed_at = None
            item.error = "Runner stopped before Contract generation completed"
            conversation = self.session.get(
                ContractConversationRecord, item.conversation_id
            )
            if conversation is not None:
                conversation.status = "generation_interrupted"
                conversation.updated_at = utc_now()
            self.interrupt_running_generation_stages(
                item.generation_attempt_id, item.error
            )
            self.append_event(
                "contract_generation",
                item.generation_attempt_id,
                "contract_generation_interrupted",
                "Contract generation 因 Runner 中断而暂停，可继续执行",
            )
        self.session.flush()
        return len(items)

    def resume_generation_attempt(
        self,
        user_id: str,
        conversation_id: str,
        generation_attempt_id: str,
    ) -> ContractGenerationAttemptRecord:
        """把被基础设施中断的原 Attempt 重新排队，不创建新 Attempt。"""
        conversation = self.get_conversation(user_id, conversation_id)
        item = self.get_generation_attempt(user_id, generation_attempt_id)
        if item.conversation_id != conversation_id:
            raise OwnershipError("Generation Attempt 不属于该 Conversation")
        if item.status in {"queued", "running", "repairing"}:
            return item
        if item.status != "interrupted":
            raise ValueError("只有基础设施中断的 Contract generation 可以继续")
        if conversation.research_run is not None:
            raise ValueError("Conversation 已进入 Research Run，不能恢复 Contract generation")
        active = self.session.scalar(
            select(ContractGenerationAttemptRecord)
            .where(
                ContractGenerationAttemptRecord.conversation_id == conversation_id,
                ContractGenerationAttemptRecord.status.in_(["queued", "running", "repairing"]),
                ContractGenerationAttemptRecord.generation_attempt_id
                != generation_attempt_id,
            )
            .limit(1)
        )
        if active is not None:
            raise ValueError("该 Contract Conversation 已有生成任务正在运行")
        item.status = "queued"
        item.error = None
        item.claimed_by_runner = None
        item.claimed_at = None
        item.completed_at = None
        conversation.status = "contract_generating"
        conversation.updated_at = utc_now()
        self.append_event(
            "contract_generation",
            generation_attempt_id,
            "contract_generation_resumed",
            "用户已从保存的 checkpoint 继续 Contract generation",
        )
        self.session.flush()
        return item

    def claim_next_generation_attempt(
        self,
        runner_id: str,
        *,
        admin_limit: int = 10,
        user_limit: int = 2,
    ) -> ContractGenerationAttemptRecord | None:
        candidate_ids = list(self.session.scalars(
            select(ContractGenerationAttemptRecord.generation_attempt_id)
            .where(
                ContractGenerationAttemptRecord.status == "queued",
                ContractGenerationAttemptRecord.claimed_by_runner.is_(None),
            )
            .order_by(ContractGenerationAttemptRecord.created_at)
        ))
        postgres = self.session.bind is not None and self.session.bind.dialect.name != "sqlite"
        for candidate_id in candidate_ids:
            item_statement = select(ContractGenerationAttemptRecord).where(
                ContractGenerationAttemptRecord.generation_attempt_id == candidate_id,
                ContractGenerationAttemptRecord.status == "queued",
                ContractGenerationAttemptRecord.claimed_by_runner.is_(None),
            )
            if postgres:
                item_statement = item_statement.with_for_update(skip_locked=True)
            item = self.session.scalar(item_statement)
            if item is None:
                continue
            user_statement = select(UserRecord).where(UserRecord.user_id == item.owner_user_id)
            if postgres:
                user_statement = user_statement.with_for_update(skip_locked=True)
            user = self.session.scalar(user_statement)
            if user is None:
                continue
            limit = max(1, int(admin_limit if user.role == "admin" else user_limit))
            active = int(self.session.scalar(
                select(func.count()).select_from(ContractGenerationAttemptRecord).where(
                    ContractGenerationAttemptRecord.owner_user_id == user.user_id,
                    ContractGenerationAttemptRecord.status.in_(["running", "repairing"]),
                    ContractGenerationAttemptRecord.claimed_by_runner.is_not(None),
                )
            ) or 0)
            if active >= limit:
                continue
            item.claimed_by_runner = runner_id
            item.claimed_at = utc_now()
            item.started_at = item.started_at or utc_now()
            item.status = "running"
            conversation = self.session.get(ContractConversationRecord, item.conversation_id)
            if conversation is not None:
                conversation.status = "contract_generating"
                conversation.updated_at = utc_now()
            self.append_event(
                "contract_generation",
                item.generation_attempt_id,
                "contract_generation_started",
                "Runner 已开始生成 Research Contract",
                {"runner_id": runner_id},
            )
            self.session.flush()
            return item
        return None

    def set_generation_attempt_status(
        self,
        generation_attempt_id: str,
        status: str,
        *,
        error: str | None = None,
        resulting_contract_version_id: str | None = None,
        event_type: str | None = None,
        event_message: str | None = None,
        event_payload: dict[str, Any] | None = None,
        runtime_retention_days: int = 7,
    ) -> ContractGenerationAttemptRecord:
        item = self.session.get(ContractGenerationAttemptRecord, generation_attempt_id)
        if item is None:
            raise FileNotFoundError("Contract generation attempt 不存在")
        item.status = status
        item.error = error
        if resulting_contract_version_id is not None:
            item.resulting_contract_version_id = resulting_contract_version_id
        if status in {"completed", "failed", "interrupted"}:
            item.completed_at = utc_now()
            item.claimed_by_runner = None
            item.claimed_at = None
        if status in {"completed", "failed"}:
            item.runtime_expires_at = utc_now() + timedelta(days=max(0, int(runtime_retention_days)))
            self.settle_generation_attempt(generation_attempt_id)
        conversation = self.session.get(ContractConversationRecord, item.conversation_id)
        if conversation is not None:
            if status == "completed":
                conversation.status = "ready"
            elif status == "failed":
                conversation.status = "generation_failed"
            elif status == "interrupted":
                conversation.status = "generation_interrupted"
            else:
                conversation.status = "contract_generating"
            conversation.updated_at = utc_now()
        if event_type:
            self.append_event(
                "contract_generation",
                generation_attempt_id,
                event_type,
                event_message or event_type,
                event_payload or {},
            )
        self.session.flush()
        return item


    # ---------- user files ----------
    def create_file_asset(
        self,
        user_id: str,
        *,
        name: str,
        media_type: str,
        payload: bytes,
    ) -> FileAssetRecord:
        user = self.get_user(user_id)
        cleaned = self.artifacts.validate_upload_name(name)
        if not user.storage_key:
            raise RuntimeError("用户缺少 storage_key")
        current_usage = self.artifacts.calculate_durable_usage(user.storage_key)
        if user.storage_quota_bytes is not None and current_usage + len(payload) > int(user.storage_quota_bytes):
            raise ValueError("Durable Storage 已达到配额，不能继续上传")
        file_id = new_id("file")
        path = self.artifacts.save_user_file(user.storage_key, file_id, cleaned, payload)
        extension = Path(cleaned).suffix.lower()
        detected_media_type = {
            ".pdf": "application/pdf",
            ".md": "text/markdown; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".tex": "application/x-tex; charset=utf-8",
        }[extension]
        item = FileAssetRecord(
            file_id=file_id,
            owner_user_id=user_id,
            original_name=cleaned,
            extension=extension,
            media_type=detected_media_type,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            artifact_path=self.artifacts.reference(path),
        )
        self.session.add(item)
        self.session.flush()
        return item

    def list_file_assets(self, user_id: str) -> list[FileAssetRecord]:
        return list(
            self.session.scalars(
                select(FileAssetRecord)
                .where(
                    FileAssetRecord.owner_user_id == user_id,
                    FileAssetRecord.deleted_at.is_(None),
                )
                .order_by(FileAssetRecord.created_at.desc())
            )
        )

    def get_file_asset(
        self, user_id: str, file_id: str, *, include_deleted: bool = False
    ) -> FileAssetRecord:
        item = self.session.get(FileAssetRecord, file_id)
        if item is None or item.owner_user_id != user_id:
            raise OwnershipError("文件不存在或不属于当前用户")
        if item.deleted_at and not include_deleted:
            raise FileNotFoundError("文件已经删除")
        return item

    def rename_file_asset(self, user_id: str, file_id: str, name: str) -> FileAssetRecord:
        """只更新文件的用户可见名称；内容、哈希和内部 Artifact 引用保持不变。"""
        item = self.get_file_asset(user_id, file_id)
        requested = name.strip()
        cleaned = self.artifacts.validate_upload_name(requested)
        if cleaned != requested:
            raise ValueError("文件名包含路径、控制字符或不允许的符号")
        if Path(cleaned).suffix.lower() != item.extension:
            raise ValueError(f"文件扩展名必须保持为 {item.extension}")
        item.original_name = cleaned
        self.session.flush()
        return item

    def delete_file_asset(self, user_id: str, file_id: str) -> None:
        item = self.get_file_asset(user_id, file_id)
        self.artifacts.delete_file(item.artifact_path)
        item.deleted_at = utc_now()
        item.artifact_path = ""
        self.session.flush()

    def source_manifest(self, user_id: str, file_ids: Iterable[str]) -> list[dict[str, Any]]:
        manifest: list[dict[str, Any]] = []
        for file_id in dict.fromkeys(file_ids):
            try:
                item = self.get_file_asset(user_id, file_id, include_deleted=True)
            except (FileNotFoundError, OwnershipError):
                manifest.append(
                    {
                        "file_id": file_id,
                        "original_name": "unknown",
                        "availability": "missing",
                    }
                )
                continue
            manifest.append(
                {
                    "file_id": item.file_id,
                    "original_name": item.original_name,
                    "extension": item.extension,
                    "media_type": item.media_type,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                    "artifact_path": item.artifact_path or None,
                    "availability": "available" if not item.deleted_at else "missing",
                }
            )
        return manifest

    # ---------- contract versions / drafts ----------
    def create_contract_version(
        self,
        conversation: ContractConversationRecord,
        *,
        title: str,
        markdown_path: Path,
        sections: dict[str, Any],
        source_manifest: list[dict[str, Any]],
        created_by: str,
        execution_policy: dict[str, Any] | None = None,
    ) -> ContractVersionRecord:
        version = conversation.latest_contract_version + 1
        item = ContractVersionRecord(
            contract_version_id=new_id("contract"),
            conversation_id=conversation.conversation_id,
            version=version,
            title=title,
            markdown_path=self.artifacts.reference(markdown_path),
            markdown_sha256=sha256_file(markdown_path),
            sections=sections,
            source_manifest=source_manifest,
            execution_policy=dict(execution_policy or {}),
            contract_language=normalize_language(conversation.contract_language),
            created_by=created_by,
        )
        self.session.add(item)
        conversation.latest_contract_version = version
        conversation.status = "ready"
        conversation.updated_at = utc_now()
        draft = self.session.get(WorkingDraftRecord, conversation.conversation_id)
        if draft is not None:
            self.session.delete(draft)
        self.session.flush()
        return item

    def get_contract_version(
        self, user_id: str, conversation_id: str, version: int | None = None
    ) -> ContractVersionRecord:
        conversation = self.get_conversation(user_id, conversation_id)
        selected = version or conversation.latest_contract_version
        item = self.session.scalar(
            select(ContractVersionRecord).where(
                ContractVersionRecord.conversation_id == conversation_id,
                ContractVersionRecord.version == selected,
            )
        )
        if item is None:
            raise FileNotFoundError(f"Contract v{selected} 不存在")
        return item

    def list_contract_versions(
        self, user_id: str, conversation_id: str
    ) -> list[ContractVersionRecord]:
        self.get_conversation(user_id, conversation_id)
        return list(
            self.session.scalars(
                select(ContractVersionRecord)
                .where(ContractVersionRecord.conversation_id == conversation_id)
                .order_by(ContractVersionRecord.version.desc())
            )
        )

    def save_working_draft(
        self,
        conversation: ContractConversationRecord,
        *,
        base_version: int,
        title: str,
        sections: dict[str, Any],
        source_manifest: list[dict[str, Any]],
        execution_policy: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> WorkingDraftRecord:
        draft = self.session.get(WorkingDraftRecord, conversation.conversation_id)
        if draft is None:
            if expected_revision not in (None, 0):
                raise ValueError("DraftRevisionConflict")
            draft = WorkingDraftRecord(
                conversation_id=conversation.conversation_id,
                base_version=base_version,
                title=title,
                sections=sections,
                source_manifest=source_manifest,
                execution_policy=dict(execution_policy or {}),
                contract_language=normalize_language(conversation.contract_language),
                draft_revision=1,
            )
            self.session.add(draft)
        else:
            if expected_revision is not None and int(draft.draft_revision) != int(expected_revision):
                raise ValueError("DraftRevisionConflict")
            draft.base_version = base_version
            draft.title = title
            draft.sections = sections
            draft.source_manifest = source_manifest
            if execution_policy is not None:
                draft.execution_policy = dict(execution_policy)
            draft.contract_language = normalize_language(conversation.contract_language)
            draft.draft_revision += 1
            draft.updated_at = utc_now()
        self.session.flush()
        return draft

    def discard_working_draft(self, user_id: str, conversation_id: str) -> None:
        self.get_conversation(user_id, conversation_id)
        draft = self.session.get(WorkingDraftRecord, conversation_id)
        if draft is not None:
            self.session.delete(draft)
            self.session.flush()

    # ---------- agent threads ----------
    def get_agent_thread(self, scope_type: str, scope_id: str, role: str) -> AgentThreadRecord | None:
        return self.session.scalar(
            select(AgentThreadRecord).where(
                AgentThreadRecord.scope_type == scope_type,
                AgentThreadRecord.scope_id == scope_id,
                AgentThreadRecord.role == role,
            )
        )

    def save_agent_thread(
        self,
        *,
        scope_type: str,
        scope_id: str,
        role: str,
        provider_thread_id: str | None,
        persistent: bool,
        model: str | None,
        reasoning_effort: str | None,
    ) -> AgentThreadRecord:
        item = self.get_agent_thread(scope_type, scope_id, role)
        if item is None:
            item = AgentThreadRecord(
                agent_thread_id=new_id("thread"),
                scope_type=scope_type,
                scope_id=scope_id,
                role=role,
                provider_thread_id=provider_thread_id,
                persistent=persistent,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            self.session.add(item)
        else:
            item.provider_thread_id = provider_thread_id
            item.persistent = persistent
            item.model = model
            item.reasoning_effort = reasoning_effort
            item.updated_at = utc_now()
        self.session.flush()
        return item

    # ---------- run shell ----------
    def create_research_run(
        self,
        conversation: ContractConversationRecord,
        contract: ContractVersionRecord,
        *,
        run_id: str | None = None,
        run_root: Path,
        contract_snapshot_path: Path,
        source_snapshot_path: Path,
        handoff_manifest_path: Path,
        expected_cost: float = 0.0,
        execution_policy: dict[str, Any] | None = None,
        research_language: str = "zh",
    ) -> tuple[ResearchRunRecord, RunAttemptRecord]:
        if conversation.research_run is not None:
            raise ValueError("该 Contract Conversation 已经存在 Research Run")
        run = ResearchRunRecord(
            run_id=run_id or new_id("run"),
            conversation_id=conversation.conversation_id,
            owner_user_id=conversation.owner_user_id,
            contract_version_id=contract.contract_version_id,
            status="queued",
            expected_cost=float(expected_cost),
            reserved_credit=0,
            precharged_credit=float(expected_cost),
            execution_policy=dict(execution_policy or {}),
            research_language=normalize_language(research_language),
            run_root=self.artifacts.reference(run_root),
            contract_snapshot_path=self.artifacts.reference(contract_snapshot_path),
            source_snapshot_path=self.artifacts.reference(source_snapshot_path),
            handoff_manifest_path=self.artifacts.reference(handoff_manifest_path),
        )
        self.session.add(run)
        self.session.flush()
        self._apply_credit_delta(
            conversation.owner_user_id,
            -self._credit(expected_cost),
            reason=f"Research Run precharge: {run.run_id}",
            source_key=f"run_precharge:{run.run_id}:attempt-1",
            run_id=run.run_id,
        )
        attempt = RunAttemptRecord(
            attempt_id=new_id("attempt"),
            run_id=run.run_id,
            attempt_number=1,
            status="created",
        )
        self.session.add(attempt)
        conversation.status = "execution_locked"
        conversation.locked_at = utc_now()
        self.session.flush()
        return run, attempt

    def create_retry_attempt(self, user_id: str, run_id: str) -> RunAttemptRecord:
        run = self.get_run(user_id, run_id)
        if run.status == "completed":
            raise ValueError("该 Contract 已合法完成，必须从 Contract 新开对话后再次执行")
        latest = max((item.attempt_number for item in run.attempts), default=0)
        if run.attempts and run.attempts[-1].status not in {"failed", "timed_out", "interrupted"}:
            raise ValueError("只有技术失败的 Attempt 可以重试")
        interrupted_precharge = run.attempts[-1].status == "interrupted" and (
            self._credit(run.precharged_credit) > 0 or self._credit(run.reserved_credit) > 0
        )
        if not interrupted_precharge and (
            self._credit(run.precharged_credit) > 0 or self._credit(run.reserved_credit) > 0
        ):
            self.settle_run_cost(run_id)
        if not interrupted_precharge:
            self.assert_can_start_run(user_id, float(run.expected_cost or 0.0))
        attempt = RunAttemptRecord(
            attempt_id=new_id("attempt"),
            run_id=run_id,
            attempt_number=latest + 1,
            status="created",
        )
        self.session.add(attempt)
        run.status = "queued"
        if not interrupted_precharge:
            run.reserved_credit = self._credit(0)
            run.precharged_credit = self._credit(run.expected_cost)
            self._apply_credit_delta(
                user_id,
                -self._credit(run.expected_cost),
                reason=f"Research Run retry precharge: {run_id} attempt {attempt.attempt_number}",
                source_key=f"run_precharge:{run_id}:{attempt.attempt_id}",
                run_id=run_id,
            )
        run.terminal_at = None
        run.graceful_drain_requested_at = None
        run.graceful_drain_requested_by = None
        run.pause_requested_at = None
        run.pause_requested_by = None
        run.paused_at = None
        run.pause_reason = None
        run.pre_pause_status = None
        run.pre_pause_phase = None
        run.resume_requested_at = None
        run.total_paused_seconds = 0
        run.pause_epoch = 0
        run.runtime_expires_at = None
        run.runtime_cleaned_at = None
        run.final_snapshot_completed_at = None
        run.claimed_by_runner = None
        run.claimed_at = None
        self.session.flush()
        return attempt

    def get_run(self, user_id: str, run_id: str) -> ResearchRunRecord:
        run = self.session.get(ResearchRunRecord, run_id)
        if run is None or run.owner_user_id != user_id:
            raise OwnershipError("Research Run 不存在或不属于当前用户")
        return run

    def delete_run(self, user_id: str, run_id: str) -> None:
        """永久删除 Run 数学内容与目录；Usage/Credit/Admin audit 不参与删除。"""
        run = self.get_run(user_id, run_id)
        if run.status in {"created", "queued", "resuming", "running", "draining", "paused"}:
            raise ValueError("活跃 Research Run 必须先终止再删除")
        root = self.artifacts.resolve(run.run_root)
        if root.is_dir() and root.is_relative_to(self.artifacts.users_root):
            import shutil

            shutil.rmtree(root)
        conversation = run.conversation
        conversation.completed_run_id = None
        conversation.status = "run_deleted"
        self.session.delete(run)
        self.session.flush()

    # ---------- events / usage ----------
    def append_event(
        self, scope_type: str, scope_id: str, kind: str, message: str, payload: dict[str, Any] | None = None
    ) -> RuntimeEventRecord:
        initial_value = int(self.session.scalar(
            select(func.coalesce(func.max(RuntimeEventRecord.sequence), 0)).where(
                RuntimeEventRecord.scope_type == scope_type,
                RuntimeEventRecord.scope_id == scope_id,
            )
        ) or 0) + 1
        sequence = self._next_scoped_sequence(
            f"runtime_event:{scope_type}", scope_id, initial_value=initial_value
        )
        event = RuntimeEventRecord(
            event_id=new_id("event"),
            scope_type=scope_type,
            scope_id=scope_id,
            sequence=sequence,
            kind=kind,
            message=message,
            payload=payload or {},
        )
        self.session.add(event)
        self.session.flush()
        return event

    def append_public_run_event(
        self,
        run_id: str,
        *,
        category: str,
        title: str,
        summary: str,
        source_role: str,
        node_id: str | None = None,
        source_key: str | None = None,
        source_created_at: str | None = None,
        summarizer: dict[str, str] | None = None,
    ) -> RuntimeEventRecord:
        """把已校验角色摘要写入独立用户事件流，内部 RuntimeEvent 保持原样。"""
        if source_role not in {"node_supervisor", "architecture_reviewer"}:
            raise ValueError("PublicEvent 只接受 Node Supervisor 或 Architecture Reviewer")
        if source_key:
            for existing in self.read_events("public_run", run_id):
                if str((existing.payload or {}).get("source_key") or "") == source_key:
                    return existing
        return self.append_event(
            "public_run",
            run_id,
            category,
            summary,
            {
                "title": title,
                "source_role": source_role,
                "node_id": node_id,
                "tone": "research",
                "source_key": source_key,
                "source_created_at": source_created_at,
                "summarizer": dict(summarizer or {}),
            },
        )

    def request_public_summary(
        self,
        source_key: str,
        *,
        run_id: str,
        mode: str,
        title: str,
        content: str,
        source_role: str | None = None,
        node_id: str | None = None,
        source_created_at: str | None = None,
    ) -> RuntimeEventRecord:
        """持久记录一个摘要请求；source_key 同时充当幂等业务主键。"""
        existing = self.read_events("public_summary", source_key)
        requested = next((item for item in existing if item.kind == "requested"), None)
        if requested is not None:
            return requested
        return self.append_event(
            "public_summary",
            source_key,
            "requested",
            content,
            {
                "run_id": run_id,
                "mode": mode,
                "title": title,
                "source_role": source_role,
                "node_id": node_id,
                "source_created_at": source_created_at,
            },
        )

    def complete_public_summary(
        self,
        source_key: str,
        summary: str,
        *,
        model: str,
        reasoning_effort: str,
        fallback: bool = False,
        error: str | None = None,
    ) -> RuntimeEventRecord:
        """保存摘要终态；重复完成返回首个结果，避免恢复时重复发布。"""
        existing = self.read_events("public_summary", source_key)
        completed = next((item for item in existing if item.kind == "completed"), None)
        if completed is not None:
            return completed
        return self.append_event(
            "public_summary",
            source_key,
            "completed",
            summary,
            {
                "model": model,
                "reasoning_effort": reasoning_effort,
                "fallback": fallback,
                "error": error,
            },
        )

    def revise_public_summary(
        self,
        source_key: str,
        summary: str,
        *,
        model: str,
        reasoning_effort: str,
        reason: str,
    ) -> RuntimeEventRecord:
        """追加摘要修订并保留旧终态，页面通过 latest 读取新版本。"""
        completed = [
            item
            for item in self.read_events("public_summary", source_key)
            if item.kind == "completed"
        ]
        previous = completed[-1] if completed else None
        return self.append_event(
            "public_summary",
            source_key,
            "completed",
            summary,
            {
                "model": model,
                "reasoning_effort": reasoning_effort,
                "fallback": False,
                "error": None,
                "revision": len(completed) + 1,
                "replaces_event_id": previous.event_id if previous is not None else None,
                "reason": reason,
            },
        )

    def latest_public_summary(self, source_key: str) -> RuntimeEventRecord | None:
        """返回某事件或 Verified Claim 的最新摘要终态。"""
        completed = [
            item
            for item in self.read_events("public_summary", source_key)
            if item.kind == "completed"
        ]
        return completed[-1] if completed else None

    def pending_public_summary_requests(self, run_id: str | None = None) -> list[RuntimeEventRecord]:
        """列出尚无 completed 终态的持久摘要请求，供 Runner 重启恢复。"""
        requests = list(
            self.session.scalars(
                select(RuntimeEventRecord)
                .where(
                    RuntimeEventRecord.scope_type == "public_summary",
                    RuntimeEventRecord.kind == "requested",
                )
                .order_by(RuntimeEventRecord.created_at)
            )
        )
        result: list[RuntimeEventRecord] = []
        for item in requests:
            payload = dict(item.payload or {})
            if run_id is not None and str(payload.get("run_id") or "") != run_id:
                continue
            if self.latest_public_summary(item.scope_id) is None:
                result.append(item)
        return result

    def read_events(self, scope_type: str, scope_id: str, after: int = 0) -> list[RuntimeEventRecord]:
        return list(
            self.session.scalars(
                select(RuntimeEventRecord)
                .where(
                    RuntimeEventRecord.scope_type == scope_type,
                    RuntimeEventRecord.scope_id == scope_id,
                    RuntimeEventRecord.sequence > after,
                )
                .order_by(RuntimeEventRecord.sequence)
            )
        )

    def latest_event_sequence(self, scope_type: str, scope_id: str) -> int:
        """返回一个事件流当前的最大 sequence，供浏览器从稳定水位续读。"""
        return int(
            self.session.scalar(
                select(func.coalesce(func.max(RuntimeEventRecord.sequence), 0)).where(
                    RuntimeEventRecord.scope_type == scope_type,
                    RuntimeEventRecord.scope_id == scope_id,
                )
            )
            or 0
        )

    def purge_contract_chat_stream_deltas(self, contract_version_id: str, stream_id: str) -> int:
        """删除已完成 Chat 流的高频 delta，保留 started/completed/failed 审计事件。"""
        rows = list(
            self.session.scalars(
                select(RuntimeEventRecord).where(
                    RuntimeEventRecord.scope_type == "contract_chat",
                    RuntimeEventRecord.scope_id == contract_version_id,
                    RuntimeEventRecord.kind.in_(["chat_delta", "chat_reset"]),
                )
            )
        )
        selected = [item for item in rows if str((item.payload or {}).get("stream_id") or "") == stream_id]
        for item in selected:
            self.session.delete(item)
        self.session.flush()
        return len(selected)

    def record_usage(
        self,
        *,
        user_id: str,
        component: str,
        role: str,
        model: str,
        usage: dict[str, Any],
        provider_thread_id: str | None = None,
        usage_semantics: str = "per_turn",
        cost_estimator: Callable[[dict[str, Any]], float] | None = None,
        conversation_id: str | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
        node_id: str | None = None,
        estimated_cost: float = 0.0,
    ) -> UsageLedgerRecord:
        """将 provider Usage 规范为单次调用增量，并同时保留原始累计计数。

        `codex exec` 在持久 thread 上报告累计计数；app-server 的 `last`
        则已经是单轮计数。累计计数只与同一 provider thread 的上一条原始
        快照做差，进程重启后仍能从 Ledger 恢复基线。
        """
        if usage_semantics not in {"per_turn", "thread_cumulative"}:
            raise ValueError(f"未知 Usage 语义：{usage_semantics}")
        reported = {
            "input_tokens": max(0, int(usage.get("input_tokens", 0) or 0)),
            "output_tokens": max(0, int(usage.get("output_tokens", 0) or 0)),
            "cached_input_tokens": max(
                0,
                int(usage.get("cached_input_tokens", usage.get("cache_read_input_tokens", 0)) or 0),
            ),
        }
        normalized = dict(reported)
        version = "turn-v1"
        if usage_semantics == "thread_cumulative" and provider_thread_id:
            previous = self.session.scalar(
                select(UsageLedgerRecord)
                .where(
                    UsageLedgerRecord.owner_user_id == user_id,
                    UsageLedgerRecord.provider_thread_id == provider_thread_id,
                    UsageLedgerRecord.usage_semantics == "thread_cumulative",
                )
                .order_by(UsageLedgerRecord.created_at.desc(), UsageLedgerRecord.usage_id.desc())
                .limit(1)
            )
            if previous is not None:
                prior = {
                    "input_tokens": int(previous.reported_input_tokens or 0),
                    "output_tokens": int(previous.reported_output_tokens or 0),
                    "cached_input_tokens": int(previous.reported_cached_input_tokens or 0),
                }
                monotone = all(reported[key] >= prior[key] for key in reported)
                if monotone:
                    normalized = {key: reported[key] - prior[key] for key in reported}
                    version = "thread-delta-v1"
                else:
                    version = "thread-reset-v1"
            else:
                version = "thread-initial-v1"
        normalized["cached_input_tokens"] = min(
            normalized["input_tokens"], normalized["cached_input_tokens"]
        )
        normalized_cost = (
            float(cost_estimator(normalized))
            if cost_estimator is not None
            else float(estimated_cost)
        )
        item = UsageLedgerRecord(
            usage_id=new_id("usage"),
            owner_user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            attempt_id=attempt_id,
            node_id=node_id,
            component=component,
            role=role,
            model=model,
            provider_thread_id=provider_thread_id,
            usage_semantics=usage_semantics,
            normalization_version=version,
            reported_input_tokens=reported["input_tokens"],
            reported_output_tokens=reported["output_tokens"],
            reported_cached_input_tokens=reported["cached_input_tokens"],
            input_tokens=normalized["input_tokens"],
            output_tokens=normalized["output_tokens"],
            cached_input_tokens=normalized["cached_input_tokens"],
            estimated_cost=normalized_cost,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def credit_consumption_windows(self, user_id: str, *, now: datetime | None = None) -> dict[str, float]:
        """以同一时刻为基准，通过账本条件求和同时计算滚动 1/7/30 天与历史消耗，不读取预扣或发放账本。"""
        reference = now if now is not None else utc_now()
        cost = UsageLedgerRecord.estimated_cost
        sums = [
            func.coalesce(func.sum(case(
                (UsageLedgerRecord.created_at >= reference - timedelta(days=days), cost),
                else_=0,
            )), 0).label(window)
            for window, days in (("1d", 1), ("7d", 7), ("30d", 30))
        ]
        values = self.session.execute(select(
            *sums, func.coalesce(func.sum(cost), 0).label("all"),
        ).where(UsageLedgerRecord.owner_user_id == user_id)).one()
        return {window: float(value) for window, value in values._mapping.items()}

    def usage_summary(self, user_id: str, *, since: datetime | None = None) -> dict[str, Any]:
        """按可选滚动时间窗汇总模型调用；账户和存储字段始终返回当前值。"""
        statement = select(UsageLedgerRecord).where(UsageLedgerRecord.owner_user_id == user_id)
        if since is not None:
            statement = statement.where(UsageLedgerRecord.created_at >= since)
        rows = list(self.session.scalars(statement))
        user = self.session.get(UserRecord, user_id)
        durable_usage = (
            self.artifacts.calculate_durable_usage(user.storage_key)
            if user is not None and user.storage_key else 0
        )
        input_tokens = sum(max(0, row.input_tokens) for row in rows)
        output_tokens = sum(max(0, row.output_tokens) for row in rows)
        cached_input_tokens = sum(
            max(0, min(row.input_tokens, row.cached_input_tokens)) for row in rows
        )
        return {
            "calls": len(rows),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_input_tokens,
            "uncached_input_tokens": max(input_tokens - cached_input_tokens, 0),
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost": round(sum(row.estimated_cost for row in rows), 6),
            "credit_balance": round(float(user.credit_balance if user else 0.0), 6),
            "reserved_credit": round(self.active_paid_reservation(user_id), 6),
            "available_credit": round(
                float(user.credit_balance) - self.active_paid_reservation(user_id)
                if user else 0.0,
                6,
            ),
            "active_runs": self.active_run_count(user_id),
            "monthly_cost": round(self.current_month_cost(user_id), 6),
            "monthly_cost_limit": float(user.monthly_cost_limit) if user and user.monthly_cost_limit is not None else None,
            "max_concurrent_research_runs": int(user.max_concurrent_research_runs) if user else 0,
            "durable_storage_bytes": durable_usage,
            "storage_quota_bytes": int(user.storage_quota_bytes) if user and user.storage_quota_bytes is not None else None,
            "storage_over_quota": bool(user and user.storage_quota_bytes is not None and durable_usage > int(user.storage_quota_bytes)),
        }

    def run_usage_summary(self, user_id: str, run_id: str) -> dict[str, int]:
        """按单个 Research Run 汇总正式 Usage Ledger，Cached 始终作为 Input 子集。"""
        self.get_run(user_id, run_id)
        rows = list(
            self.session.scalars(
                select(UsageLedgerRecord).where(
                    UsageLedgerRecord.owner_user_id == user_id,
                    UsageLedgerRecord.run_id == run_id,
                )
            )
        )
        input_tokens = sum(max(0, row.input_tokens) for row in rows)
        output_tokens = sum(max(0, row.output_tokens) for row in rows)
        cached_input_tokens = sum(
            max(0, min(row.input_tokens, row.cached_input_tokens)) for row in rows
        )
        return {
            "calls": len(rows),
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
        }

    # ---------- Credit precharge / runner queue ----------
    def _apply_credit_delta(
        self,
        user_id: str,
        amount: Decimal,
        *,
        reason: str,
        source_key: str,
        run_id: str | None = None,
    ) -> CreditLedgerRecord | None:
        """原子修改余额并以 source_key 保证预扣、退款和补扣幂等。"""
        delta = self._credit(amount)
        if delta == 0:
            return None
        existing = self.session.scalar(
            select(CreditLedgerRecord).where(CreditLedgerRecord.source_key == source_key)
        )
        if existing is not None:
            return existing
        user = self.session.get(UserRecord, user_id)
        if user is None:
            raise FileNotFoundError(f"用户不存在：{user_id}")
        user.credit_balance = self._credit(user.credit_balance) + delta
        entry = CreditLedgerRecord(
            credit_event_id=new_id("credit"),
            user_id=user_id,
            subject_email=user.email,
            subject_storage_key=user.storage_key,
            amount=delta,
            reason=reason,
            run_id=run_id,
            source_key=source_key,
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    def _settle_precharged_operation(
        self,
        user_id: str,
        *,
        precharged: Decimal,
        actual: Decimal,
        previously_settled: Decimal,
        operation: str,
        operation_id: str,
        run_id: str | None = None,
    ) -> None:
        """将本次新增实际费用与已真实预扣额比较，生成一次退款或补扣。"""
        incremental_actual = max(self._credit(0), actual - previously_settled)
        adjustment = self._credit(precharged) - incremental_actual
        if adjustment == 0:
            return
        action = "refund" if adjustment > 0 else "overage"
        self._apply_credit_delta(
            user_id,
            adjustment,
            reason=f"{operation} settlement {action}: {operation_id}",
            source_key=f"{operation.lower().replace(' ', '_')}_settlement:{operation_id}:{actual}",
            run_id=run_id,
        )

    def active_run_reservation(self, user_id: str) -> float:
        value = self.session.scalar(select(func.coalesce(func.sum(ResearchRunRecord.reserved_credit), 0.0)).where(
            ResearchRunRecord.owner_user_id == user_id,
            ResearchRunRecord.status.in_(["created", "queued", "resuming", "running", "draining", "paused"]),
        ))
        return float(value or 0.0)

    def active_generation_reservation(self, user_id: str) -> float:
        value = self.session.scalar(select(func.coalesce(func.sum(ContractGenerationAttemptRecord.reserved_credit), 0)).where(
            ContractGenerationAttemptRecord.owner_user_id == user_id,
            ContractGenerationAttemptRecord.status.in_(["queued", "running", "repairing", "interrupted"]),
        ))
        return float(value or 0)

    def active_chat_reservation(self, user_id: str) -> float:
        value = self.session.scalar(select(func.coalesce(func.sum(ContractChatCallRecord.reserved_credit), 0)).where(
            ContractChatCallRecord.owner_user_id == user_id,
            ContractChatCallRecord.status.in_(["reserved", "running"]),
        ))
        return float(value or 0)

    def active_paid_reservation(self, user_id: str) -> float:
        """仅返回迁移前仍未结算的 legacy reservation。"""
        return (
            self.active_run_reservation(user_id)
            + self.active_generation_reservation(user_id)
            + self.active_chat_reservation(user_id)
        )

    def active_precharged_credit(self, user_id: str) -> float:
        """返回已经真实扣款、但任务尚未终态结算的预扣总额。"""
        run_value = self.session.scalar(select(func.coalesce(func.sum(ResearchRunRecord.precharged_credit), 0)).where(
            ResearchRunRecord.owner_user_id == user_id,
            ResearchRunRecord.status.in_(["created", "queued", "resuming", "running", "draining", "paused"]),
        ))
        generation_value = self.session.scalar(select(func.coalesce(func.sum(ContractGenerationAttemptRecord.precharged_credit), 0)).where(
            ContractGenerationAttemptRecord.owner_user_id == user_id,
            ContractGenerationAttemptRecord.status.in_(["queued", "running", "repairing", "interrupted"]),
        ))
        chat_value = self.session.scalar(select(func.coalesce(func.sum(ContractChatCallRecord.precharged_credit), 0)).where(
            ContractChatCallRecord.owner_user_id == user_id,
            ContractChatCallRecord.status.in_(["reserved", "running"]),
        ))
        return float((run_value or 0) + (generation_value or 0) + (chat_value or 0))

    def active_run_count(self, user_id: str) -> int:
        return int(self.session.scalar(select(func.count()).select_from(ResearchRunRecord).where(
            ResearchRunRecord.owner_user_id == user_id,
            ResearchRunRecord.status.in_(["created", "queued", "resuming", "running", "draining", "paused"]),
        )) or 0)

    def available_credit(self, user_id: str) -> float:
        user = self.get_user(user_id)
        return float(user.credit_balance) - self.active_paid_reservation(user_id)

    def current_month_cost(self, user_id: str) -> float:
        now = utc_now()
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        value = self.session.scalar(select(func.coalesce(func.sum(UsageLedgerRecord.estimated_cost), 0.0)).where(
            UsageLedgerRecord.owner_user_id == user_id,
            UsageLedgerRecord.created_at >= month_start,
        ))
        return float(value or 0.0)

    def assert_can_start_run(self, user_id: str, expected_cost: float) -> None:
        # Serialize Credit admission and upfront debit for the same user on PostgreSQL.
        # Without this row lock, two simultaneous approvals can both observe the same
        # precharge balance and over-admit. SQLite tests remain portable.
        statement = select(UserRecord).where(UserRecord.user_id == user_id)
        if self.session.bind is not None and self.session.bind.dialect.name != "sqlite":
            statement = statement.with_for_update()
        user = self.session.scalar(statement)
        if user is None or user.status != "active":
            raise FileNotFoundError(f"用户不存在或不可用：{user_id}")
        expected = max(0.0, float(expected_cost))
        if user.storage_key and user.storage_quota_bytes is not None:
            if self.artifacts.calculate_durable_usage(user.storage_key) > int(user.storage_quota_bytes):
                raise ValueError("Durable Storage 已超额，不能启动新的 Research Run")
        run_limit = int(user.max_concurrent_research_runs)
        if self.active_run_count(user_id) >= run_limit:
            raise ValueError("已达到同时运行 Research Run 上限")
        if self.available_credit(user_id) < expected:
            raise ValueError("可用 Credit 不足以覆盖本 Contract 的预计花费")
        if user.monthly_cost_limit is not None:
            committed = self.current_month_cost(user_id) + self.active_paid_reservation(user_id) + self.active_precharged_credit(user_id)
            if committed + expected > float(user.monthly_cost_limit):
                raise ValueError("本月已用费用与活跃 Run 预计额度将超过 monthly cost limit")

    def assert_can_start_paid_operation(self, user_id: str, expected_credit: float) -> None:
        """串行检查一次非 Run 付费操作的余额和月度额度。"""
        statement = select(UserRecord).where(UserRecord.user_id == user_id)
        if self.session.bind is not None and self.session.bind.dialect.name != "sqlite":
            statement = statement.with_for_update()
        user = self.session.scalar(statement)
        if user is None or user.status != "active":
            raise FileNotFoundError(f"用户不存在或不可用：{user_id}")
        expected = max(0.0, float(expected_credit))
        if float(user.credit_balance) - self.active_paid_reservation(user_id) < expected:
            raise ValueError("可用 Credit 不足以覆盖本次模型调用的预计花费")
        if user.monthly_cost_limit is not None:
            committed = self.current_month_cost(user_id) + self.active_paid_reservation(user_id) + self.active_precharged_credit(user_id)
            if committed + expected > float(user.monthly_cost_limit):
                raise ValueError("本月已用费用与活跃预扣将超过 monthly cost limit")

    def create_contract_chat_call(
        self,
        user_id: str,
        conversation_id: str,
        contract_version_id: str,
        *,
        profile: str,
        expected_credit: float,
        idempotency_key: str,
    ) -> ContractChatCallRecord:
        existing = self.session.scalar(select(ContractChatCallRecord).where(
            ContractChatCallRecord.idempotency_key == idempotency_key
        ))
        if existing is not None:
            if existing.owner_user_id != user_id:
                raise OwnershipError("Contract Chat 幂等键不属于当前用户")
            return existing
        self.get_conversation(user_id, conversation_id)
        self.assert_can_start_paid_operation(user_id, expected_credit)
        item = ContractChatCallRecord(
            chat_call_id=new_id("chatcall"), conversation_id=conversation_id,
            contract_version_id=contract_version_id, owner_user_id=user_id,
            idempotency_key=idempotency_key, profile=profile, status="reserved",
            expected_credit=self._credit(expected_credit),
            reserved_credit=self._credit(0),
            precharged_credit=self._credit(expected_credit),
        )
        self.session.add(item)
        self.session.flush()
        self._apply_credit_delta(
            user_id,
            -self._credit(expected_credit),
            reason=f"Contract Chat precharge: {item.chat_call_id}",
            source_key=f"chat_precharge:{item.chat_call_id}",
        )
        return item

    def settle_contract_chat_call(self, chat_call_id: str, *, status: str, error: str | None = None) -> float:
        item = self.session.get(ContractChatCallRecord, chat_call_id)
        if item is None:
            raise FileNotFoundError("Contract Chat call 不存在")
        actual = self._credit(self.session.scalar(select(func.coalesce(func.sum(UsageLedgerRecord.estimated_cost), 0)).where(
            UsageLedgerRecord.attempt_id == chat_call_id
        )))
        already = self._credit(item.settled_credit)
        self._settle_precharged_operation(
            item.owner_user_id,
            precharged=self._credit(item.precharged_credit),
            actual=actual,
            previously_settled=already,
            operation="Contract Chat",
            operation_id=chat_call_id,
        )
        item.actual_credit = actual; item.settled_credit = actual
        item.reserved_credit = self._credit(0); item.precharged_credit = self._credit(0); item.status = status
        item.error = error; item.completed_at = utc_now()
        self.session.flush()
        return float(actual)

    def settle_generation_attempt(self, generation_attempt_id: str) -> float:
        item = self.session.get(ContractGenerationAttemptRecord, generation_attempt_id)
        if item is None:
            raise FileNotFoundError("Contract generation attempt 不存在")
        actual = self._credit(self.session.scalar(select(func.coalesce(func.sum(UsageLedgerRecord.estimated_cost), 0)).where(
            UsageLedgerRecord.attempt_id == generation_attempt_id
        )))
        already = self._credit(item.settled_credit)
        self._settle_precharged_operation(
            item.owner_user_id,
            precharged=self._credit(item.precharged_credit),
            actual=actual,
            previously_settled=already,
            operation="Contract generation",
            operation_id=generation_attempt_id,
        )
        item.actual_credit = actual; item.settled_credit = actual
        item.reserved_credit = self._credit(0); item.precharged_credit = self._credit(0)
        self.session.flush()
        return float(actual)

    def release_stale_run_claims(self) -> int:
        runs = list(self.session.scalars(select(ResearchRunRecord).where(
            ResearchRunRecord.status.in_(["queued", "resuming"]),
            ResearchRunRecord.claimed_by_runner.is_not(None),
        )))
        for run in runs:
            run.claimed_by_runner = None
            run.claimed_at = None
        self.session.flush()
        return len(runs)

    def claim_next_queued_run(
        self,
        runner_id: str,
        *,
        admin_limit: int = 8,
        user_limit: int = 2,
    ) -> ResearchRunRecord | None:
        """领取最早的 owner-eligible Run；满额用户不会阻塞其他用户。"""
        candidate_ids = list(self.session.scalars(
            select(ResearchRunRecord.run_id).where(
                ResearchRunRecord.status.in_(["queued", "resuming"]),
                ResearchRunRecord.claimed_by_runner.is_(None),
            ).order_by(ResearchRunRecord.created_at)
        ))
        postgres = self.session.bind is not None and self.session.bind.dialect.name != "sqlite"
        for candidate_id in candidate_ids:
            run_statement = select(ResearchRunRecord).where(
                ResearchRunRecord.run_id == candidate_id,
                ResearchRunRecord.status.in_(["queued", "resuming"]),
                ResearchRunRecord.claimed_by_runner.is_(None),
            )
            if postgres:
                run_statement = run_statement.with_for_update(skip_locked=True)
            run = self.session.scalar(run_statement)
            if run is None:
                continue
            user_statement = select(UserRecord).where(UserRecord.user_id == run.owner_user_id)
            if postgres:
                user_statement = user_statement.with_for_update(skip_locked=True)
            user = self.session.scalar(user_statement)
            if user is None:
                continue
            limit = max(1, int(admin_limit if user.role == "admin" else user_limit))
            active = int(self.session.scalar(
                select(func.count()).select_from(ResearchRunRecord).where(
                    ResearchRunRecord.owner_user_id == user.user_id,
                    or_(
                        ResearchRunRecord.status.in_(["running", "draining"]),
                        (
                            (ResearchRunRecord.status.in_(["queued", "resuming"]))
                            & ResearchRunRecord.claimed_by_runner.is_not(None)
                        ),
                    ),
                )
            ) or 0)
            if active >= limit:
                continue
            run.claimed_by_runner = runner_id
            run.claimed_at = utc_now()
            run.last_heartbeat_at = utc_now()
            self.session.flush()
            return run
        return None

    # ---------- Pausable Agent operations ----------
    def begin_agent_operation(
        self,
        *,
        operation_id: str,
        run_id: str,
        attempt_id: str,
        node_id: str | None,
        operation_kind: str,
        role: str,
        role_key: str,
        scope_type: str,
        scope_id: str,
        task_path: Path,
        payload: dict[str, Any] | None = None,
        pause_epoch: int = 0,
    ) -> AgentOperationCheckpointRecord:
        """建立或恢复一个业务调用 checkpoint，并冻结原始任务正文。"""
        item = self.session.get(AgentOperationCheckpointRecord, operation_id)
        if item is None:
            item = AgentOperationCheckpointRecord(
                operation_id=operation_id,
                run_id=run_id,
                attempt_id=attempt_id,
                node_id=node_id,
                operation_kind=operation_kind,
                role=role,
                role_key=role_key,
                scope_type=scope_type,
                scope_id=scope_id,
                task_path=self.artifacts.reference(task_path),
                task_sha256=sha256_file(task_path),
                payload=dict(payload or {}),
                pause_epoch=pause_epoch,
            )
            self.session.add(item)
        else:
            item.status = "running"
            item.error = None
            item.pause_epoch = pause_epoch
            item.updated_at = utc_now()
        self.session.flush()
        return item

    def update_agent_operation_thread(
        self, operation_id: str, provider_thread_id: str
    ) -> None:
        """在线程刚创建时立即持久化真实 provider thread ID。"""
        item = self.session.get(AgentOperationCheckpointRecord, operation_id)
        if item is None:
            raise FileNotFoundError("Agent operation checkpoint 不存在")
        item.provider_thread_id = provider_thread_id
        item.updated_at = utc_now()
        self.session.flush()

    def set_agent_operation_status(
        self,
        operation_id: str,
        status: str,
        *,
        repair_number: int | None = None,
        error: str | None = None,
    ) -> None:
        """持久更新 operation 状态；只有 completed 才写终态时间。"""
        item = self.session.get(AgentOperationCheckpointRecord, operation_id)
        if item is None:
            return
        item.status = status
        item.error = error
        if repair_number is not None:
            item.repair_number = repair_number
        item.updated_at = utc_now()
        item.completed_at = utc_now() if status == "completed" else None
        self.session.flush()

    def paused_agent_operations(self, run_id: str) -> list[AgentOperationCheckpointRecord]:
        """按创建顺序返回一个 Run 尚未完成的暂停业务调用。"""
        return list(self.session.scalars(
            select(AgentOperationCheckpointRecord)
            .where(
                AgentOperationCheckpointRecord.run_id == run_id,
                AgentOperationCheckpointRecord.status.in_(["paused", "capacity_wait"]),
            )
            .order_by(AgentOperationCheckpointRecord.created_at)
        ))

    def active_candidate_verification(
        self, run_id: str, candidate_artifact_id: str
    ) -> AgentOperationCheckpointRecord | None:
        """查找当前暂停代次中同一 Candidate 尚未结束的 fresh 核验。"""
        run = self.session.get(ResearchRunRecord, run_id)
        if run is None or not candidate_artifact_id:
            return None
        return self.session.scalar(
            select(AgentOperationCheckpointRecord)
            .where(
                AgentOperationCheckpointRecord.run_id == run_id,
                AgentOperationCheckpointRecord.role_key
                == f"claim_verifier:{candidate_artifact_id}",
                AgentOperationCheckpointRecord.pause_epoch == run.pause_epoch,
                AgentOperationCheckpointRecord.status.in_(
                    ["running", "paused", "capacity_wait"]
                ),
            )
            .order_by(AgentOperationCheckpointRecord.created_at.desc())
            .limit(1)
        )

    def heartbeat_runner(self, runner_id: str, *, status: str, current_run_id: str | None = None, metadata: dict[str, Any] | None = None) -> RunnerHeartbeatRecord:
        item = self.session.get(RunnerHeartbeatRecord, runner_id)
        if item is None:
            item = RunnerHeartbeatRecord(runner_id=runner_id)
            self.session.add(item)
        item.status = status
        item.current_run_id = current_run_id
        item.metadata_json = metadata or {}
        item.last_heartbeat_at = utc_now()
        self.session.flush()
        return item

    def settle_run_cost(self, run_id: str) -> float:
        run = self.session.get(ResearchRunRecord, run_id)
        if run is None:
            raise FileNotFoundError("Run 不存在")
        actual = self._credit(self.session.scalar(select(func.coalesce(func.sum(UsageLedgerRecord.estimated_cost), 0)).where(UsageLedgerRecord.run_id == run_id)))
        already_settled = self._credit(run.settled_cost)
        latest_attempt = max(run.attempts, key=lambda item: item.attempt_number, default=None)
        settlement_id = f"{run_id}:{latest_attempt.attempt_id if latest_attempt else 'initial'}"
        self._settle_precharged_operation(
            run.owner_user_id,
            precharged=self._credit(run.precharged_credit),
            actual=actual,
            previously_settled=already_settled,
            operation="Research Run",
            operation_id=settlement_id,
            run_id=run_id,
        )
        run.actual_cost = actual
        run.settled_cost = actual
        run.reserved_credit = self._credit(0)
        run.precharged_credit = self._credit(0)
        self.session.flush()
        return float(actual)

    # ---------- prover records ----------
    def create_node(
        self,
        run: ResearchRunRecord,
        *,
        kind: str,
        title: str,
        objective: str,
        completion_criterion: str,
        details: dict[str, Any] | None = None,
        parent_node_id: str | None = None,
        origin_finding_id: str | None = None,
        origin_review_id: str | None = None,
        status: str = "ready",
    ) -> ResearchNodeRecord:
        if kind not in {"discovery", "claim"}:
            raise ValueError("Research Node 只能是 discovery 或 claim")
        node = ResearchNodeRecord(
            node_id=new_id("node"),
            run_id=run.run_id,
            parent_node_id=parent_node_id,
            kind=kind,
            title=title,
            objective=objective,
            completion_criterion=completion_criterion,
            status=status,
            details=details or {},
            origin_finding_id=origin_finding_id,
            origin_review_id=origin_review_id,
            graph_version_created=run.graph_version,
        )
        self.session.add(node)
        self.session.flush()
        return node

    def list_nodes(self, run_id: str) -> list[ResearchNodeRecord]:
        return list(
            self.session.scalars(
                select(ResearchNodeRecord)
                .where(ResearchNodeRecord.run_id == run_id)
                .order_by(ResearchNodeRecord.created_at)
            )
        )

    def active_nodes(self, run_id: str) -> list[ResearchNodeRecord]:
        return list(
            self.session.scalars(
                select(ResearchNodeRecord).where(
                    ResearchNodeRecord.run_id == run_id,
                    ResearchNodeRecord.status.in_(["ready", "active", "draining"]),
                )
            )
        )

    def create_edge(
        self,
        run_id: str,
        source_node_id: str,
        target_node_id: str,
        relation: str,
        condition: str = "",
    ) -> ResearchGraphEdgeRecord:
        if relation not in {"requires", "informs", "alternative_to", "conditional_on"}:
            raise ValueError("未知 Research Graph edge")
        edge = ResearchGraphEdgeRecord(
            edge_id=new_id("edge"),
            run_id=run_id,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            relation=relation,
            condition=condition,
        )
        self.session.add(edge)
        self.session.flush()
        return edge

    def list_workers(self, node_id: str, *, include_retired: bool = True) -> list[WorkerIdentityRecord]:
        statement = select(WorkerIdentityRecord).where(WorkerIdentityRecord.node_id == node_id)
        if not include_retired:
            statement = statement.where(WorkerIdentityRecord.status != "retired")
        return list(self.session.scalars(statement.order_by(WorkerIdentityRecord.created_at)))

    def get_worker(self, worker_id: str) -> WorkerIdentityRecord:
        item = self.session.get(WorkerIdentityRecord, worker_id)
        if item is None:
            raise FileNotFoundError("Worker identity 不存在")
        return item

    def get_worker_by_label(self, node_id: str, worker_label: str) -> WorkerIdentityRecord | None:
        return self.session.scalar(select(WorkerIdentityRecord).where(
            WorkerIdentityRecord.node_id == node_id,
            WorkerIdentityRecord.worker_label == worker_label,
        ))

    def create_worker(self, node: ResearchNodeRecord, *, persona: str, worker_label: str) -> WorkerIdentityRecord:
        existing = self.get_worker_by_label(node.node_id, worker_label)
        if existing is not None:
            if existing.persona != persona:
                raise ValueError("同一 worker label 不能改变 Persona")
            return existing
        worker = WorkerIdentityRecord(
            worker_id=new_id("worker"), node_id=node.node_id, persona=persona,
            worker_label=worker_label, status="idle",
        )
        self.session.add(worker)
        self.session.flush()
        return worker

    def retire_worker(self, worker_id: str) -> WorkerIdentityRecord:
        worker = self.get_worker(worker_id)
        worker.status = "retired"
        worker.retired_at = utc_now()
        self.session.flush()
        return worker

    def list_assignments(self, node_id: str) -> list[WorkerAssignmentRecord]:
        return list(self.session.scalars(select(WorkerAssignmentRecord).where(
            WorkerAssignmentRecord.node_id == node_id
        ).order_by(WorkerAssignmentRecord.created_at)))

    def current_assignment(self, worker_id: str) -> WorkerAssignmentRecord | None:
        return self.session.scalar(select(WorkerAssignmentRecord).where(
            WorkerAssignmentRecord.worker_id == worker_id,
            WorkerAssignmentRecord.status.in_(["ready", "running"]),
        ).order_by(WorkerAssignmentRecord.assignment_version.desc()).limit(1))

    def create_assignment(self, node: ResearchNodeRecord, *, worker: WorkerIdentityRecord | None = None, persona: str | None = None, worker_label: str | None = None, task_markdown: str, assignment_version: int | None = None) -> WorkerAssignmentRecord:
        if worker is None:
            if not persona or not worker_label:
                raise ValueError("创建 assignment 必须提供 worker 或 persona+worker_label")
            worker = self.create_worker(node, persona=persona, worker_label=worker_label)
        if worker.node_id != node.node_id:
            raise ValueError("Worker 不属于当前 Node")
        if worker.status in {"running", "draining", "retired"}:
            raise ValueError(f"Worker 当前不可接收新任务：{worker.status}")
        if self.current_assignment(worker.worker_id) is not None:
            raise ValueError("Worker 已有未完成 assignment；系统不建立 pending backlog")
        version = assignment_version or worker.assignment_counter + 1
        worker.assignment_counter = version
        worker.status = "assigned"
        assignment = WorkerAssignmentRecord(
            assignment_id=new_id("assignment"), node_id=node.node_id,
            worker_id=worker.worker_id, persona=worker.persona,
            worker_label=worker.worker_label, assignment_version=version,
            task_markdown=task_markdown, status="ready",
        )
        self.session.add(assignment)
        self.session.flush()
        return assignment

    def add_worker_report(self, assignment: WorkerAssignmentRecord, *, status: str, major_event: bool, markdown_path: Path, node_version: int, graph_version: int) -> WorkerReportRecord:
        report_number = assignment.report_count + 1
        report = WorkerReportRecord(
            report_id=new_id("report"), assignment_id=assignment.assignment_id,
            report_number=report_number, assignment_version=assignment.assignment_version,
            node_version=node_version, graph_version=graph_version, status=status,
            major_event=major_event, markdown_path=self.artifacts.reference(markdown_path), sha256=sha256_file(markdown_path),
        )
        assignment.report_count = report_number
        assignment.status = "completed" if status in {"completed", "partial"} else "reported"
        assignment.completed_at = utc_now()
        worker = self.get_worker(assignment.worker_id)
        if worker.status == "draining":
            worker.status = "retired"
            worker.retired_at = utc_now()
        elif worker.status != "retired":
            worker.status = "idle"
        self.session.add(report)
        self.session.flush()
        return report

    def pending_worker_reports(self, node_id: str) -> list[WorkerReportRecord]:
        """Return the durable Supervisor inbox for one Research Node."""
        return list(self.session.scalars(
            select(WorkerReportRecord)
            .join(WorkerAssignmentRecord)
            .where(
                WorkerAssignmentRecord.node_id == node_id,
                WorkerReportRecord.consumption_status == "pending_supervisor_review",
            )
            .order_by(WorkerReportRecord.created_at, WorkerReportRecord.report_id)
        ))

    def consume_worker_reports(
        self,
        node_id: str,
        report_ids: list[str],
        *,
        supervisor_review_id: str,
    ) -> None:
        """Mark exactly the reports included in a successfully applied Local Review."""
        if not report_ids:
            return
        now = utc_now()
        reports = self.session.scalars(
            select(WorkerReportRecord)
            .join(WorkerAssignmentRecord)
            .where(
                WorkerAssignmentRecord.node_id == node_id,
                WorkerReportRecord.report_id.in_(report_ids),
                WorkerReportRecord.consumption_status == "pending_supervisor_review",
            )
        )
        for report in reports:
            report.consumption_status = "consumed"
            report.consumed_by_supervisor_review_id = supervisor_review_id
            report.consumed_at = now
        self.session.flush()

    def add_finding(
        self,
        node: ResearchNodeRecord,
        *,
        finding_type: str,
        title: str,
        markdown: str,
        significance: str,
        source_report_id: str | None = None,
        nominated: bool = False,
        rationale: str = "",
        recommended_actions: list[dict[str, Any]] | None = None,
    ) -> ResearchFindingRecord:
        finding = ResearchFindingRecord(
            finding_id=new_id("finding"),
            node_id=node.node_id,
            source_report_id=source_report_id,
            finding_type=finding_type,
            title=title,
            markdown=markdown,
            significance=significance,
            status="nominated" if nominated else "local",
        )
        self.session.add(finding)
        self.session.flush()
        if nominated:
            proposal = PromotionProposalRecord(
                proposal_id=new_id("promotion"),
                finding_id=finding.finding_id,
                supervisor_rationale=rationale,
                recommended_actions=recommended_actions or [],
            )
            self.session.add(proposal)
            self.session.flush()
        return finding

    def pending_promotions(self, run_id: str) -> list[PromotionProposalRecord]:
        return list(
            self.session.scalars(
                select(PromotionProposalRecord)
                .join(ResearchFindingRecord)
                .join(ResearchNodeRecord)
                .where(
                    ResearchNodeRecord.run_id == run_id,
                    PromotionProposalRecord.status == "pending",
                )
                .order_by(PromotionProposalRecord.created_at)
            )
        )

    def add_architecture_review(
        self,
        run: ResearchRunRecord,
        *,
        trigger: str,
        promotion_slots: int,
        decision_path: Path,
        actions: list[dict[str, Any]],
        graph_version_before: int,
        graph_version_after: int,
        review_id: str | None = None,
        provider_thread_id: str | None = None,
        trigger_reasons: list[str] | None = None,
        input_snapshot_revision: int | None = None,
        input_node_preconditions: dict[str, Any] | None = None,
        application_status: str = "applied",
        input_delta_ids: list[str] | None = None,
        input_communication_ids: list[str] | None = None,
        evidence_index_path: Path | None = None,
        evidence_inspected: dict[str, Any] | None = None,
        snapshot_update_markdown: str = "",
        ledger_updates_markdown: str = "",
    ) -> ArchitectureReviewRecord:
        initial_value = int(self.session.scalar(
            select(func.coalesce(func.max(ArchitectureReviewRecord.sequence), 0)).where(
                ArchitectureReviewRecord.run_id == run.run_id
            )
        ) or 0) + 1
        sequence = self._next_scoped_sequence(
            "architecture_review", run.run_id, initial_value=initial_value
        )
        review = ArchitectureReviewRecord(
            review_id=review_id or new_id("review"),
            run_id=run.run_id,
            sequence=sequence,
            trigger=trigger,
            graph_version_before=graph_version_before,
            graph_version_after=graph_version_after,
            promotion_slots=promotion_slots,
            decision_markdown_path=self.artifacts.reference(decision_path),
            actions=actions,
            provider_thread_id=provider_thread_id,
            trigger_reasons=trigger_reasons or [],
            input_snapshot_revision=input_snapshot_revision,
            input_node_preconditions=input_node_preconditions or {},
            application_status=application_status,
            input_delta_ids=input_delta_ids or [],
            input_communication_ids=input_communication_ids or [],
            evidence_index_path=(self.artifacts.reference(evidence_index_path) if evidence_index_path else None),
            evidence_index_sha256=(sha256_file(evidence_index_path) if evidence_index_path else None),
            evidence_inspected=evidence_inspected or {},
            snapshot_update_markdown=snapshot_update_markdown,
            ledger_updates_markdown=ledger_updates_markdown,
        )
        self.session.add(review)
        self.session.flush()
        return review

    def upsert_architecture_snapshot(
        self,
        run: ResearchRunRecord,
        *,
        payload: dict[str, Any],
        snapshot_path: Path,
        last_review_research_seconds: float | None = None,
        root_progress: bool = False,
    ) -> ArchitectureSnapshotRecord:
        """原子覆盖当前全局白板，并保留单调 revision。"""
        snapshot = self.session.get(ArchitectureSnapshotRecord, run.run_id)
        if snapshot is None:
            snapshot = ArchitectureSnapshotRecord(
                run_id=run.run_id,
                revision=1,
                graph_version=run.graph_version,
                payload=payload,
                snapshot_markdown_path=self.artifacts.reference(snapshot_path),
                sha256=sha256_file(snapshot_path),
                last_review_research_seconds=float(last_review_research_seconds or 0),
                last_root_progress_at=utc_now() if root_progress else run.started_at,
            )
            self.session.add(snapshot)
        else:
            snapshot.revision += 1
            snapshot.graph_version = run.graph_version
            snapshot.payload = payload
            snapshot.snapshot_markdown_path = self.artifacts.reference(snapshot_path)
            snapshot.sha256 = sha256_file(snapshot_path)
            if last_review_research_seconds is not None:
                snapshot.last_review_research_seconds = float(last_review_research_seconds)
            if root_progress:
                snapshot.last_root_progress_at = utc_now()
        self.session.flush()
        return snapshot

    def add_global_state_delta(
        self,
        run: ResearchRunRecord,
        *,
        change_kind: str,
        node: ResearchNodeRecord | None = None,
        old_summary: dict[str, Any] | None = None,
        new_summary: dict[str, Any] | None = None,
        evidence_refs: list[str] | None = None,
        gap_key: str | None = None,
        root_relevant: bool = False,
    ) -> GlobalStateDeltaRecord:
        """追加一条不带 Reviewer 请求语义的客观状态变化。"""
        delta = GlobalStateDeltaRecord(
            delta_id=new_id("delta"),
            run_id=run.run_id,
            node_id=node.node_id if node is not None else None,
            node_version=node.node_version if node is not None else None,
            graph_version=run.graph_version,
            change_kind=change_kind,
            old_summary=old_summary or {},
            new_summary=new_summary or {},
            evidence_refs=evidence_refs or [],
            gap_key=gap_key,
            root_relevant=root_relevant,
        )
        self.session.add(delta)
        self.session.flush()
        return delta

    def pending_global_state_deltas(self, run_id: str) -> list[GlobalStateDeltaRecord]:
        return list(self.session.scalars(
            select(GlobalStateDeltaRecord).where(
                GlobalStateDeltaRecord.run_id == run_id,
                GlobalStateDeltaRecord.status == "pending",
            ).order_by(GlobalStateDeltaRecord.created_at)
        ))

    def add_architecture_communication(
        self,
        run: ResearchRunRecord,
        node: ResearchNodeRecord,
        *,
        attention: str,
        title: str,
        question: str,
        global_consequence: str,
        affected_node_ids: list[str],
        related_evidence_ids: list[str],
        local_pressure_tests: str,
        rationale: str,
        source_path: Path,
    ) -> ArchitectureCommunicationRecord:
        """把 Supervisor 的显式全局问题写入可恢复 Inbox。"""
        item = ArchitectureCommunicationRecord(
            communication_id=new_id("communication"),
            run_id=run.run_id,
            node_id=node.node_id,
            node_version=node.node_version,
            graph_version=run.graph_version,
            attention=attention,
            title=title,
            question=question,
            global_consequence=global_consequence,
            affected_node_ids=affected_node_ids,
            related_evidence_ids=related_evidence_ids,
            local_pressure_tests=local_pressure_tests,
            rationale=rationale,
            source_markdown_path=self.artifacts.reference(source_path),
            source_sha256=sha256_file(source_path),
        )
        self.session.add(item)
        self.session.flush()
        return item

    def pending_architecture_communications(self, run_id: str) -> list[ArchitectureCommunicationRecord]:
        return list(self.session.scalars(
            select(ArchitectureCommunicationRecord).where(
                ArchitectureCommunicationRecord.run_id == run_id,
                ArchitectureCommunicationRecord.status == "pending",
            ).order_by(ArchitectureCommunicationRecord.created_at)
        ))

    def consume_architecture_inputs(
        self,
        review_id: str,
        *,
        delta_ids: list[str],
        communication_ids: list[str],
    ) -> None:
        """仅在 Reviewer 成功后消费本轮冻结输入。"""
        now = utc_now()
        if delta_ids:
            for item in self.session.scalars(select(GlobalStateDeltaRecord).where(
                GlobalStateDeltaRecord.delta_id.in_(delta_ids),
                GlobalStateDeltaRecord.status == "pending",
            )):
                item.status = "consumed"
                item.consumed_by_review_id = review_id
                item.consumed_at = now
        if communication_ids:
            for item in self.session.scalars(select(ArchitectureCommunicationRecord).where(
                ArchitectureCommunicationRecord.communication_id.in_(communication_ids),
                ArchitectureCommunicationRecord.status == "pending",
            )):
                item.status = "consumed"
                item.consumed_by_review_id = review_id
                item.resolved_at = now

    def mark_stale_architecture_communications(self, run_id: str) -> list[str]:
        """Only explicit terminal invalidation makes a pending communication stale."""
        stale: list[str] = []
        for item in self.pending_architecture_communications(run_id):
            node = self.session.get(ResearchNodeRecord, item.node_id)
            if (
                node is None
                or node.status in {"retired", "failed"}
            ):
                item.status = "stale"
                item.resolved_at = utc_now()
                stale.append(item.communication_id)
        return stale

    def append_architecture_ledger(
        self,
        run: ResearchRunRecord,
        *,
        source_review_id: str,
        title: str,
        markdown: str,
    ) -> ArchitectureDecisionLedgerRecord:
        initial_value = int(self.session.scalar(
            select(func.coalesce(func.max(ArchitectureDecisionLedgerRecord.sequence), 0)).where(
                ArchitectureDecisionLedgerRecord.run_id == run.run_id
            )
        ) or 0) + 1
        sequence = self._next_scoped_sequence(
            "architecture_ledger", run.run_id, initial_value=initial_value
        )
        entry = ArchitectureDecisionLedgerRecord(
            ledger_id=new_id("ledger"),
            run_id=run.run_id,
            sequence=sequence,
            source_review_id=source_review_id,
            graph_version=run.graph_version,
            title=title,
            markdown=markdown,
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    def list_architecture_ledger(self, run_id: str) -> list[ArchitectureDecisionLedgerRecord]:
        return list(self.session.scalars(
            select(ArchitectureDecisionLedgerRecord).where(
                ArchitectureDecisionLedgerRecord.run_id == run_id
            ).order_by(ArchitectureDecisionLedgerRecord.sequence)
        ))

    def find_verified_claim_for_candidate(
        self,
        run_id: str,
        node_id: str,
        *,
        candidate_artifact_id: str = "",
        candidate_sha256: str = "",
        statement_sha256: str = "",
    ) -> VerifiedClaimRecord | None:
        """按 Candidate ID、内容 SHA 或同 Node 精确命题 SHA 查找既有 PASS。"""
        identities = []
        if candidate_artifact_id:
            identities.append(
                VerifiedClaimRecord.candidate_artifact_id == candidate_artifact_id
            )
        if candidate_sha256:
            identities.append(VerifiedClaimRecord.sha256 == candidate_sha256)
        if statement_sha256:
            identities.append(
                VerifiedClaimRecord.statement_sha256 == statement_sha256
            )
        if not identities:
            return None
        return self.session.scalar(
            select(VerifiedClaimRecord)
            .where(
                VerifiedClaimRecord.run_id == run_id,
                VerifiedClaimRecord.node_id == node_id,
                VerifiedClaimRecord.verdict == "PASS",
                or_(*identities),
            )
            .order_by(VerifiedClaimRecord.created_at)
            .limit(1)
        )

    def add_verified_claim(
        self,
        run_id: str,
        node_id: str,
        *,
        verdict: str,
        statement: str,
        candidate_path: Path,
        verifier_report_path: Path,
        candidate_artifact_id: str | None = None,
        statement_sha256: str | None = None,
        proof_sha256: str | None = None,
        candidate_scope: str = "auxiliary_fact",
        controlling_proposition_id: str | None = None,
        reuse_boundary: str = "",
        node_version: int | None = None,
        graph_version: int | None = None,
        verification_policy_version: str = "claim-v2",
        certificate_path: Path | None = None,
    ) -> VerifiedClaimRecord:
        """登记一条核验结果；新生产路径同时保存不可变 Candidate 身份。"""
        node = self.session.get(ResearchNodeRecord, node_id)
        claim = VerifiedClaimRecord(
            verified_claim_id=new_id("claim"),
            run_id=run_id,
            node_id=node_id,
            candidate_artifact_id=candidate_artifact_id,
            verdict=verdict,
            title=node.title if node is not None else "Verified mathematical result",
            statement=statement,
            statement_sha256=statement_sha256,
            proof_sha256=proof_sha256,
            candidate_sha256=sha256_file(candidate_path),
            verifier_report_sha256=sha256_file(verifier_report_path),
            candidate_scope=candidate_scope,
            controlling_proposition_id=controlling_proposition_id,
            reuse_boundary=reuse_boundary,
            node_version=node_version,
            graph_version=graph_version,
            verification_policy_version=verification_policy_version,
            certificate_path=(
                self.artifacts.reference(certificate_path) if certificate_path else None
            ),
            candidate_path=self.artifacts.reference(candidate_path),
            verifier_report_path=self.artifacts.reference(verifier_report_path),
            sha256=sha256_file(candidate_path),
        )
        self.session.add(claim)
        self.session.flush()
        return claim

    def publication_state(
        self, run_id: str, attempt_id: str
    ) -> PublicationStateRecord | None:
        return self.session.scalar(select(PublicationStateRecord).where(
            PublicationStateRecord.run_id == run_id,
            PublicationStateRecord.attempt_id == attempt_id,
        ))

    def authorize_publication(
        self,
        run_id: str,
        attempt_id: str,
        completion_kind: str,
        source: str,
    ) -> tuple[PublicationStateRecord, bool]:
        """Create one authoritative publication authorization per Run attempt."""
        existing = self.publication_state(run_id, attempt_id)
        if existing is not None:
            if existing.completion_kind != completion_kind or existing.source != source:
                raise ValueError("同一 Attempt 存在冲突的 Publication Authorization")
            return existing, False
        publication_id = new_id("publication")
        values = {
            "publication_id": publication_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "completion_kind": completion_kind,
            "source": source,
            "status": "authorized",
            "authorized_at": utc_now(),
            "updated_at": utc_now(),
        }
        bind = self.session.get_bind()
        if bind.dialect.name == "postgresql":
            statement = postgresql_insert(PublicationStateRecord).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=["run_id", "attempt_id"]
            )
        elif bind.dialect.name == "sqlite":
            statement = sqlite_insert(PublicationStateRecord).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=["run_id", "attempt_id"]
            )
        else:  # pragma: no cover
            statement = None
        if statement is None:
            item = PublicationStateRecord(**values)
            self.session.add(item)
        else:
            self.session.execute(statement)
        self.session.flush()
        item = self.publication_state(run_id, attempt_id)
        if item is None:  # pragma: no cover
            raise RuntimeError("Publication Authorization 写入失败")
        if item.completion_kind != completion_kind or item.source != source:
            raise ValueError("同一 Attempt 存在冲突的 Publication Authorization")
        return item, item.publication_id == publication_id

    def claim_publication(
        self, run_id: str, attempt_id: str, *, claimed_by: str
    ) -> bool:
        """Atomically allow exactly one caller to enter the expensive Writer chain."""
        now = utc_now()
        claimed = self.session.scalar(
            update(PublicationStateRecord)
            .where(
                PublicationStateRecord.run_id == run_id,
                PublicationStateRecord.attempt_id == attempt_id,
                PublicationStateRecord.status == "authorized",
            )
            .values(
                status="publishing", claimed_by=claimed_by,
                publishing_at=now, updated_at=now, error=None,
            )
            .returning(PublicationStateRecord.publication_id)
        )
        self.session.flush()
        return claimed is not None

    def finish_publication(
        self,
        run_id: str,
        attempt_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        item = self.publication_state(run_id, attempt_id)
        if item is None:
            raise FileNotFoundError("Publication State 不存在")
        item.status = status
        item.error = error
        item.updated_at = utc_now()
        if status == "published":
            item.published_at = utc_now()
        self.session.flush()

    def list_verified_claims(self, run_id: str) -> list[VerifiedClaimRecord]:
        return list(
            self.session.scalars(
                select(VerifiedClaimRecord)
                .where(VerifiedClaimRecord.run_id == run_id)
                .order_by(VerifiedClaimRecord.created_at)
            )
        )
