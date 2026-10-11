from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import hashlib
import hmac
import secrets
import smtplib
import ssl
import re
from typing import Protocol

from email_validator import EmailNotValidError, validate_email
import socks

from .db import Database, utc_now
from .repositories import RepositoryHub
from .artifacts import ArtifactStore
from .settings import VNextSettings




def _as_utc(value: datetime) -> datetime:
    """SQLite may return timezone-naive datetimes even for timezone-aware columns."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Mailer(Protocol):
    def send_otp(self, email: str, code: str, expires_minutes: int) -> None: ...


class ConsoleMailer:
    """Development mailer. It never contacts the network."""

    def __init__(self) -> None:
        self.outbox: list[dict[str, str]] = []

    def send_otp(self, email: str, code: str, expires_minutes: int) -> None:
        self.outbox.append({"email": email, "code": code, "expires_minutes": str(expires_minutes)})
        print(f"[MathLab DEV OTP] to={email} code={code} expires={expires_minutes}m", flush=True)


class SmtpMailer:
    def __init__(self, settings: VNextSettings) -> None:
        self.settings = settings

    def send_otp(self, email: str, code: str, expires_minutes: int) -> None:
        message = EmailMessage()
        message["Subject"] = "Your MathLab verification code"
        message["From"] = self.settings.smtp_from
        message["To"] = email
        message.set_content(
            "Your MathLab verification code is:\n\n"
            f"{code}\n\n"
            f"This code expires in {expires_minutes} minutes.\n"
            "If you did not request this code, you can ignore this email."
        )
        mode = self.settings.smtp_tls_mode.lower()
        if mode == "ssl":
            client_class = _SocksSMTPSSL if self.settings.smtp_socks_host else smtplib.SMTP_SSL
            kwargs = {
                "context": ssl.create_default_context(),
                "timeout": 20,
            }
            if self.settings.smtp_socks_host:
                kwargs.update({
                    "proxy_host": self.settings.smtp_socks_host,
                    "proxy_port": self.settings.smtp_socks_port,
                })
            with client_class(
                self.settings.smtp_host,
                self.settings.smtp_port,
                **kwargs,
            ) as client:
                self._login(client)
                client.send_message(message)
            return
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as client:
            if mode in {"starttls", "tls"}:
                client.starttls(context=ssl.create_default_context())
            self._login(client)
            client.send_message(message)

    def _login(self, client: smtplib.SMTP) -> None:
        if self.settings.smtp_username:
            client.login(self.settings.smtp_username, self.settings.smtp_password)


class _SocksSMTPSSL(smtplib.SMTP_SSL):
    """只为当前 SMTP 连接创建 SOCKS5 socket，不修改进程全局网络栈。"""

    def __init__(self, *args, proxy_host: str, proxy_port: int, **kwargs) -> None:
        self._proxy_host = proxy_host
        self._proxy_port = int(proxy_port)
        super().__init__(*args, **kwargs)

    def _get_socket(self, host: str, port: int, timeout: float):
        raw = socks.create_connection(
            (host, port), timeout=timeout,
            proxy_type=socks.SOCKS5,
            proxy_addr=self._proxy_host,
            proxy_port=self._proxy_port,
            proxy_rdns=True,
        )
        return self.context.wrap_socket(raw, server_hostname=host)


@dataclass(frozen=True)
class OtpRequestResult:
    email: str
    expires_in_seconds: int
    dev_code: str | None = None


class AuthService:
    def __init__(
        self,
        settings: VNextSettings,
        database: Database,
        artifacts: ArtifactStore,
        mailer: Mailer | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.artifacts = artifacts
        if mailer is not None:
            self.mailer = mailer
        elif settings.smtp_mode.lower() in {"console", "mock", "local"}:
            self.mailer = ConsoleMailer()
        else:
            self.mailer = SmtpMailer(settings)

    def _hash(self, code: str, salt: str) -> str:
        # A six-digit OTP can be brute-forced from a leaked database if it is only
        # salted.  HMAC with a deployment secret keeps database-only disclosure
        # insufficient to recover outstanding codes.
        payload = f"{salt}:{code}".encode("utf-8")
        return hmac.new(self.settings.auth_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    def _session_hash(self, session_token: str) -> str:
        """Store only a keyed digest of bearer session tokens in PostgreSQL."""
        return hmac.new(
            self.settings.auth_secret.encode("utf-8"),
            f"session:{session_token}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _normalize_email(email: str) -> str:
        try:
            return validate_email(email.strip(), check_deliverability=False).normalized.lower()
        except EmailNotValidError as exc:
            raise ValueError("邮箱格式无效") from exc

    def request_otp(self, email: str) -> OtpRequestResult:
        normalized = self._normalize_email(email)
        now = utc_now()
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(16)
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            repo.reserve_otp_send(
                normalized,
                now=now,
                cooldown_seconds=self.settings.otp_resend_cooldown_seconds,
                daily_limit=self.settings.otp_daily_send_limit,
            )
            otp = repo.create_otp(
                normalized,
                self._hash(code, salt),
                salt,
                now + timedelta(seconds=self.settings.otp_expiry_seconds),
                self.settings.otp_max_attempts,
            )
            otp_id = otp.otp_id
            repo.bind_otp_send_reservation(normalized, otp_id)
        try:
            self.mailer.send_otp(
                normalized,
                code,
                max(1, self.settings.otp_expiry_seconds // 60),
            )
        except Exception as exc:
            with self.database.session() as session:
                RepositoryHub(session, self.artifacts).mark_otp_delivery_failed(
                    otp_id, exc.__class__.__name__
                )
                RepositoryHub(session, self.artifacts).release_otp_send_reservation(
                    normalized, otp_id
                )
            raise ValueError("验证码邮件投递失败，请立即重试") from exc
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            repo.mark_otp_delivered(otp_id)
            repo.release_otp_send_reservation(normalized, otp_id)
        return OtpRequestResult(
            email=normalized,
            expires_in_seconds=self.settings.otp_expiry_seconds,
            dev_code=code if self.settings.dev_mode and isinstance(self.mailer, ConsoleMailer) else None,
        )

    def verify_otp(self, email: str, code: str) -> tuple[str, str]:
        normalized = self._normalize_email(email)
        if not re.fullmatch(r"\d{6}", code.strip()):
            raise ValueError("验证码必须是 6 位数字")
        error: str | None = None
        result: tuple[str, str] | None = None
        # Invalid attempts and expiry consumption are security state and therefore must
        # commit even though the public API returns an error.  Raising inside the
        # Database.session() context would roll these counters back.
        with self.database.session() as session:
            repo = RepositoryHub(session, self.artifacts)
            otp = repo.latest_otp(normalized)
            now = utc_now()
            if otp is None:
                error = "没有可用验证码，请重新发送"
            elif otp.status != "delivered":
                error = "验证码尚未成功投递，请重新发送"
            elif _as_utc(otp.expires_at) <= now:
                otp.used_at = now
                error = "验证码已过期"
            elif otp.attempt_count >= otp.max_attempts:
                otp.used_at = now
                error = "验证码尝试次数已耗尽"
            else:
                otp.attempt_count += 1
                supplied = self._hash(code.strip(), otp.salt)
                if not hmac.compare_digest(supplied, otp.code_hash):
                    if otp.attempt_count >= otp.max_attempts:
                        otp.used_at = now
                    error = "验证码错误"
                else:
                    otp.used_at = now
                    user = repo.get_or_create_email_user(
                        normalized,
                        initial_credit=self.settings.new_user_initial_credit,
                        bootstrap_admin_email=self.settings.bootstrap_admin_email,
                        bootstrap_admin_credit=self.settings.bootstrap_admin_credit,
                        default_storage_quota_bytes=self.settings.default_storage_quota_bytes,
                        bootstrap_admin_max_concurrent_runs=self.settings.developer_max_concurrent_runs,
                        default_user_max_concurrent_runs=self.settings.user_max_concurrent_runs,
                    )
                    session_id = secrets.token_urlsafe(48)
                    repo.create_auth_session(
                        user.user_id,
                        self._session_hash(session_id),
                        now + timedelta(seconds=self.settings.session_ttl_seconds),
                    )
                    result = (user.user_id, session_id)
        if error is not None:
            raise ValueError(error)
        if result is None:  # defensive; all branches above set error or result
            raise RuntimeError("OTP verification produced no result")
        return result

    def resolve_session(self, session_id: str) -> str:
        with self.database.session() as session:
            return RepositoryHub(session, self.artifacts).resolve_auth_session(self._session_hash(session_id)).user_id

    def logout(self, session_id: str) -> None:
        with self.database.session() as session:
            RepositoryHub(session, self.artifacts).revoke_auth_session(self._session_hash(session_id))
