from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import json
import os
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib
from urllib.parse import urlparse


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip().lower().lstrip("@") for part in (value or "").split(",") if part.strip())


@dataclass(frozen=True)
class VNextSettings:
    """Central vNext product, auth, agent and deployment settings."""

    root: Path
    name: str
    host: str
    port: int
    open_browser: bool
    data_root: Path
    database_url: str
    database_echo: bool
    auto_create_schema: bool
    dev_mode: bool
    test_auth_bypass: bool
    offline_agents: bool
    process_role: str
    planner_config: Path
    prover_config: Path
    contract_repair_attempts: int
    contract_integrator_repair_attempts: int
    local_review_report_threshold: int
    architecture_review_stage_threshold: int
    max_parallel_research_nodes: int
    max_promotions_per_review: int
    max_workers_per_research_node: int
    internal_max_parallel_agent_calls: int
    max_local_reviews_per_node: int
    default_run_seconds: int
    graceful_shutdown_soft_seconds: int
    graceful_shutdown_hard_seconds: int
    node_drain_soft_seconds: int
    node_drain_hard_seconds: int
    runner_poll_seconds: float
    runner_max_concurrent_runs: int
    runner_id: str
    allowed_email_domains: tuple[str, ...]
    otp_expiry_seconds: int
    otp_max_attempts: int
    otp_resend_cooldown_seconds: int
    otp_daily_send_limit: int
    session_ttl_seconds: int
    session_cookie_name: str
    csrf_cookie_name: str
    session_cookie_secure: bool
    auth_secret: str
    bootstrap_admin_email: str
    bootstrap_admin_credit: float
    new_user_initial_credit: float
    smtp_mode: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from: str
    smtp_tls_mode: str
    smtp_socks_host: str
    smtp_socks_port: int
    default_storage_quota_bytes: int
    runtime_retention_days: int
    runtime_gc_poll_seconds: float
    developer_active_run_override: bool
    developer_max_concurrent_runs: int
    require_live_credit_pricing: bool
    contract_generation_reserves: dict[str, float]
    contract_generation_estimated_minutes: dict[str, int]
    contract_chat_reserves: dict[str, float]
    expected_cost_cost_priority: float
    expected_cost_balanced: float
    expected_cost_capability: float
    model_pricing: dict[str, dict[str, float]]
    contract_generation_max_concurrent: int = 2
    admin_contract_generation_max_concurrent: int = 10
    user_contract_generation_max_concurrent: int = 2
    admin_runner_max_concurrent_runs: int = 8
    user_runner_max_concurrent_runs: int = 2
    user_max_concurrent_runs: int = 2
    compute_tool_timeout_seconds: int = 30
    latex_compile_timeout_seconds: int = 120
    public_summary_timeout_seconds: int = 180
    execution_model_tiers: dict[str, dict[str, object]] = field(default_factory=dict)
    execution_duration_tiers: dict[str, dict[str, object]] = field(default_factory=dict)
    execution_price_table: dict[str, dict[str, float]] = field(default_factory=dict)
    execution_pricing_version: str = "dev-v3"
    publication_estimate_seconds: int = 2700
    architecture_review_interval_seconds: int = 900
    architecture_review_communication_threshold: int = 3
    architecture_review_distinct_node_threshold: int = 2
    architecture_shared_bottleneck_node_threshold: int = 2
    architecture_root_stagnation_seconds: int = 1800
    seed_dev_users: bool = False

    def estimate_turn_cost(self, model: str, usage: dict[str, object]) -> float:
        """Return explicit provider cost or a configurable token-price estimate.

        Pricing values are per one million tokens and are intentionally deployment
        configuration: MathLab cannot assume a third-party provider's current rates.
        """
        for key in ("credit_cost", "billed_credits"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and float(value) > 0:
                return float(value)
        price = self.model_pricing.get(model) or self.model_pricing.get("*")
        if not price:
            if self.require_live_credit_pricing and not self.offline_agents and (
                int(usage.get("input_tokens", 0) or 0)
                or int(usage.get("output_tokens", 0) or 0)
            ):
                raise RuntimeError(f"模型 `{model}` 尚未配置 Credit/token 费率")
            return 0.0
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        cached_tokens = int(usage.get("cached_input_tokens", usage.get("cache_read_input_tokens", 0)) or 0)
        cached_tokens = max(0, min(input_tokens, cached_tokens))
        uncached = max(0, input_tokens - cached_tokens)
        return (
            uncached * float(price.get("input_per_million", 0.0))
            + cached_tokens * float(price.get("cached_input_per_million", price.get("input_per_million", 0.0)))
            + output_tokens * float(price.get("output_per_million", 0.0))
        ) / 1_000_000.0

    def require_models_priced(self, models: list[str] | tuple[str, ...] | set[str]) -> None:
        """在 live 付费操作启动前确认每个实际模型均有 Credit/token 费率。"""
        if self.offline_agents or not self.require_live_credit_pricing:
            return
        missing = sorted({model for model in models if model and model not in self.model_pricing and "*" not in self.model_pricing})
        if missing:
            raise RuntimeError("以下模型尚未配置 Credit/token 费率：" + "、".join(missing))

    def contract_reserve(self, profile: str, *, chat: bool = False) -> float:
        """返回 Contract generation 或单次 Chat 的 admission reserve。"""
        table = self.contract_chat_reserves if chat else self.contract_generation_reserves
        key = profile if profile in table else "high"
        return max(0.0, float(table.get(key, 0.0)))

    @property
    def max_active_nodes(self) -> int:  # compatibility alias
        return self.max_parallel_research_nodes

    @property
    def max_workers_per_node(self) -> int:  # compatibility alias
        return self.max_workers_per_research_node

    @property
    def max_parallel_agent_calls(self) -> int:  # internal-only compatibility alias
        return self.internal_max_parallel_agent_calls

    def contract_generation_limit(self, role: str) -> int:
        """返回单个 owner 可同时执行的 Contract generation 数。"""
        return max(1, self.admin_contract_generation_max_concurrent if role == "admin" else self.user_contract_generation_max_concurrent)

    def runner_run_limit(self, role: str) -> int:
        """返回单个 owner 可同时由 Runner 执行的 Prover Run 数。"""
        return max(1, self.admin_runner_max_concurrent_runs if role == "admin" else self.user_runner_max_concurrent_runs)

    def execution_catalog(self) -> dict[str, object]:
        model_tiers = self.execution_model_tiers or {
            "gpt_low": {"label": "GPT Low", "live_mapping": "", "description": "全程采用 GPT-5.6 Luna Max 覆盖研究、核验与论文整理，适合以较低成本快速判断问题是否可做、主要研究路线是否成立。"},
            "gpt_medium": {"label": "GPT Medium", "live_mapping": "", "description": "以 Sol 高强度能力为核心，分支探索采用 Luna Max，兼顾研究质量与计算成本；这是性价比最高的方案。"},
            "gpt_high": {"label": "GPT High", "live_mapping": "", "description": "以 Sol 最高强度能力覆盖主要研究过程；适合追求最高结果质量。"},
            "qwen_only": {"label": "Qwen only", "live_mapping": "", "description": "Qwen 3.8 Max 推理速度较慢、价格较高，实测表现接近 GPT Low。建议仅在 GPT 模型暂不可用时作为备选方案。"},
        }
        duration_tiers = self.execution_duration_tiers or {
            "quick_1h": {"label": "Quick · 1 hour", "duration_seconds": 3600},
            "balanced_3h": {"label": "Balanced · 3 hours", "duration_seconds": 10800},
            "detailed_6h": {"label": "Detailed · 6 hours", "duration_seconds": 21600},
        }
        prices = self.execution_price_table or {
            "gpt_low": {"quick_1h": 2.0, "balanced_3h": 4.5, "detailed_6h": 9.0},
            "gpt_medium": {"quick_1h": 15.0, "balanced_3h": 40.0, "detailed_6h": 80.0},
            "gpt_high": {"quick_1h": 20.0, "balanced_3h": 50.0, "detailed_6h": 100.0},
            "qwen_only": {"quick_1h": 12.0, "balanced_3h": 30.0, "detailed_6h": 60.0},
        }
        return {
            "model_tiers": model_tiers,
            "duration_tiers": duration_tiers,
            "price_table": prices,
            "pricing_version": self.execution_pricing_version,
            "publication_estimate_seconds": self.publication_estimate_seconds,
        }

    def make_execution_policy(
        self,
        model_tier: str | None = None,
        duration_tier: str | None = None,
        *,
        require_live_mapping: bool = False,
    ) -> dict[str, object]:
        catalog = self.execution_catalog()
        model_tiers = dict(catalog["model_tiers"])
        duration_tiers = dict(catalog["duration_tiers"])
        legacy_model_aliases = {
            "economy": "qwen_only",
            "cost_priority": "qwen_only",
            "balanced": "gpt_medium",
            "capability": "gpt_medium",
            "capability_priority": "gpt_medium",
            "deepseek_only": "qwen_only",
            "gpt_deepseek_mixture": "gpt_medium",
            "gpt_qwen_mixture": "gpt_medium",
        }
        legacy_duration_aliases = {
            "small": "quick_1h",
            "standard": "balanced_3h",
            "large": "balanced_3h",
        }
        raw_model = str(model_tier or "").strip()
        raw_duration = str(duration_tier or "").strip()
        normalized_model = legacy_model_aliases.get(raw_model, raw_model)
        normalized_duration = legacy_duration_aliases.get(raw_duration, raw_duration)
        if raw_model and normalized_model not in model_tiers:
            raise ValueError(f"未知 execution model tier: {raw_model}")
        if raw_duration and normalized_duration not in duration_tiers:
            raise ValueError(f"未知 execution duration tier: {raw_duration}")
        model_key = normalized_model or ("gpt_medium" if "gpt_medium" in model_tiers else next(iter(model_tiers)))
        duration_key = normalized_duration or next(iter(duration_tiers))
        model_payload = dict(model_tiers[model_key])
        duration_payload = dict(duration_tiers[duration_key])
        live_mapping = str(model_payload.get("live_mapping") or "")
        if require_live_mapping and not self.offline_agents and not live_mapping:
            raise RuntimeError(f"Execution model tier `{model_key}` 尚未配置 live mapping")
        prices = dict(dict(catalog["price_table"]).get(model_key) or {})
        estimated = float(prices.get(duration_key, 0.0))
        return {
            "model_tier": model_key,
            "duration_tier": duration_key,
            "duration_seconds": int(duration_payload.get("duration_seconds", self.default_run_seconds)),
            "pricing_version": str(catalog["pricing_version"]),
            "estimated_cost": estimated,
            "live_mapping": live_mapping,
            "publication_estimate_seconds": self.publication_estimate_seconds,
        }

    def with_offline(self, enabled: bool) -> "VNextSettings":
        return replace(self, offline_agents=enabled)


def load_settings(path: Path) -> VNextSettings:
    resolved = path.expanduser().resolve()
    with resolved.open("rb") as handle:
        data = tomllib.load(handle)
    root = resolved.parent
    app = data["application"]
    storage = data.get("storage") or {}
    database = data.get("database") or {}
    components = data.get("components") or {}
    runtime = data.get("vnext_runtime") or {}
    auth = data.get("auth") or {}
    smtp = data.get("smtp") or {}
    costs = data.get("expected_costs") or {}
    paid = data.get("paid_operations") or {}
    pricing_payload = data.get("model_pricing") or {}
    pricing_override = os.environ.get("MATHLAB_MODEL_PRICING_JSON", "").strip()
    if pricing_override:
        parsed = json.loads(pricing_override)
        if not isinstance(parsed, dict):
            raise RuntimeError("MATHLAB_MODEL_PRICING_JSON 必须是 JSON object")
        pricing_payload = parsed
    model_pricing = {
        str(model): {str(key): float(value) for key, value in dict(payload).items()}
        for model, payload in dict(pricing_payload).items()
    }
    execution = data.get("execution_policy") or {}
    default_model_tiers = {
        "gpt_low": {"label": "GPT Low", "live_mapping": "", "description": "全程采用 GPT-5.6 Luna Max 覆盖研究、核验与论文整理，适合以较低成本快速判断问题是否可做、主要研究路线是否成立。"},
        "gpt_medium": {"label": "GPT Medium", "live_mapping": "", "description": "以 Sol 高强度能力为核心，分支探索采用 Luna Max，兼顾研究质量与计算成本；这是性价比最高的方案。"},
        "gpt_high": {"label": "GPT High", "live_mapping": "", "description": "以 Sol 最高强度能力覆盖主要研究过程；适合追求最高结果质量。"},
        "qwen_only": {"label": "Qwen only", "live_mapping": "", "description": "Qwen 3.8 Max 推理速度较慢、价格较高，实测表现接近 GPT Low。建议仅在 GPT 模型暂不可用时作为备选方案。"},
    }
    default_duration_tiers = {
        "quick_1h": {"label": "Quick · 1 hour", "duration_seconds": 3600},
        "balanced_3h": {"label": "Balanced · 3 hours", "duration_seconds": 10800},
        "detailed_6h": {"label": "Detailed · 6 hours", "duration_seconds": 21600},
    }
    execution_model_tiers = dict(execution.get("model_tiers") or default_model_tiers)
    execution_duration_tiers = dict(execution.get("duration_tiers") or default_duration_tiers)
    raw_price_table = dict(execution.get("price_table") or {})
    execution_price_table = {
        str(model): {str(duration): float(value) for duration, value in dict(prices).items()}
        for model, prices in raw_price_table.items()
    }

    data_root_raw = os.environ.get("MATHLAB_DATA_ROOT", str(Path(storage.get("data_root", "~/.local/share/mathlab")).expanduser()))
    data_root = Path(data_root_raw).expanduser().resolve()
    url_env = str(database.get("url_env", "MATHLAB_DATABASE_URL"))
    database_url = os.environ.get(url_env) or str(database.get("url", "postgresql+psycopg://mathlab:mathlab@127.0.0.1:5432/mathlab"))
    env = os.environ.get("MATHLAB_ENV", "development")
    offline = os.environ.get("MATHLAB_OFFLINE_AGENTS", "0") == "1"
    domains = _csv(os.environ.get("MATHLAB_ALLOWED_EMAIL_DOMAINS")) or tuple(str(item).lower().lstrip("@") for item in auth.get("allowed_email_domains", ["local.mathlab"]))
    auth_secret = os.environ.get("MATHLAB_AUTH_SECRET", str(auth.get("auth_secret", "local-only-placeholder")))
    if env == "production" and (not auth_secret or auth_secret == "local-only-placeholder"):
        raise RuntimeError("生产环境必须设置非默认 MATHLAB_AUTH_SECRET")
    smtp_socks_host = os.environ.get("MATHLAB_SMTP_SOCKS_HOST", str(smtp.get("socks_host", ""))).strip()
    smtp_socks_port = int(os.environ.get("MATHLAB_SMTP_SOCKS_PORT", smtp.get("socks_port", 1080)))
    if not smtp_socks_host:
        proxy = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or ""
        parsed_proxy = urlparse(proxy)
        if parsed_proxy.scheme.lower() in {"socks5", "socks5h"} and parsed_proxy.hostname:
            smtp_socks_host = parsed_proxy.hostname
            smtp_socks_port = int(parsed_proxy.port or 1080)

    return VNextSettings(
        root=root,
        name=str(app["name"]),
        host=str(app.get("host", "127.0.0.1")),
        port=int(app.get("port", 3264)),
        open_browser=bool(app.get("open_browser", True)),
        data_root=data_root,
        database_url=database_url,
        database_echo=bool(database.get("echo", False)),
        auto_create_schema=os.environ.get(
            "MATHLAB_AUTO_CREATE_SCHEMA",
            "1" if bool(database.get("auto_create_schema", env != "production")) else "0",
        ) == "1",
        dev_mode=env != "production",
        test_auth_bypass=os.environ.get("MATHLAB_TEST_AUTH_BYPASS", "0") == "1",
        offline_agents=offline,
        process_role=os.environ.get("MATHLAB_PROCESS_ROLE", "combined"),
        planner_config=(root / components.get("planner_config", "planner/config.toml")).resolve(),
        prover_config=(root / components.get("prover_config", "prover/config.toml")).resolve(),
        contract_repair_attempts=int(runtime.get("contract_repair_attempts", 5)),
        contract_integrator_repair_attempts=int(runtime.get("contract_integrator_repair_attempts", 50)),
        local_review_report_threshold=int(runtime.get("local_review_report_threshold", 2)),
        architecture_review_stage_threshold=int(runtime.get("architecture_review_stage_threshold", 2)),
        max_parallel_research_nodes=int(runtime.get("max_parallel_research_nodes", runtime.get("max_active_nodes", 10))),
        max_promotions_per_review=int(runtime.get("max_promotions_per_review", 2)),
        max_workers_per_research_node=int(runtime.get("max_workers_per_research_node", runtime.get("max_workers_per_node", 7))),
        internal_max_parallel_agent_calls=int(runtime.get("internal_max_parallel_agent_calls", runtime.get("max_parallel_agent_calls", 100))),
        max_local_reviews_per_node=int(runtime.get("max_local_reviews_per_node", 40)),
        default_run_seconds=int(runtime.get("default_run_seconds", 14400)),
        graceful_shutdown_soft_seconds=int(runtime.get("graceful_shutdown_soft_seconds", 30)),
        graceful_shutdown_hard_seconds=int(runtime.get("graceful_shutdown_hard_seconds", 90)),
        node_drain_soft_seconds=int(runtime.get("node_drain_soft_seconds", 30)),
        node_drain_hard_seconds=int(runtime.get("node_drain_hard_seconds", 120)),
        runner_poll_seconds=float(runtime.get("runner_poll_seconds", 1.0)),
        runner_max_concurrent_runs=int(runtime.get("runner_max_concurrent_runs", 2)),
        runner_id=os.environ.get("MATHLAB_RUNNER_ID", "runner-local"),
        allowed_email_domains=domains,
        otp_expiry_seconds=int(auth.get("otp_expiry_seconds", 600)),
        otp_max_attempts=int(auth.get("otp_max_attempts", 5)),
        otp_resend_cooldown_seconds=int(auth.get("otp_resend_cooldown_seconds", 60)),
        otp_daily_send_limit=int(auth.get("otp_daily_send_limit", 10)),
        session_ttl_seconds=int(auth.get("session_ttl_seconds", 60 * 60 * 24 * 14)),
        session_cookie_name=str(auth.get("session_cookie_name", "mathlab_session")),
        csrf_cookie_name=str(auth.get("csrf_cookie_name", "mathlab_csrf")),
        session_cookie_secure=os.environ.get(
            "MATHLAB_SESSION_COOKIE_SECURE",
            "1" if bool(auth.get("session_cookie_secure", env == "production")) else "0",
        ) == "1",
        auth_secret=auth_secret,
        bootstrap_admin_email=os.environ.get("MATHLAB_BOOTSTRAP_ADMIN_EMAIL", str(auth.get("bootstrap_admin_email", ""))).strip().lower(),
        bootstrap_admin_credit=float(os.environ.get("MATHLAB_BOOTSTRAP_ADMIN_CREDIT", auth.get("bootstrap_admin_credit", 1000.0))),
        new_user_initial_credit=float(os.environ.get("MATHLAB_NEW_USER_INITIAL_CREDIT", auth.get("new_user_initial_credit", 0.0))),
        smtp_mode=os.environ.get("MATHLAB_SMTP_MODE", str(smtp.get("mode", "console"))),
        smtp_host=os.environ.get("MATHLAB_SMTP_HOST", str(smtp.get("host", "mailpit"))),
        smtp_port=int(os.environ.get("MATHLAB_SMTP_PORT", smtp.get("port", 1025))),
        smtp_username=(
            os.environ.get("MATHLAB_SMTP_USERNAME")
            or os.environ.get("MATHLAB_EMAIL_ADDRESS")
            or str(smtp.get("username", ""))
        ),
        smtp_password=(
            os.environ.get("MATHLAB_SMTP_PASSWORD")
            or os.environ.get("MATHLAB_EMAIL_AUTH_CODE")
            or str(smtp.get("password", ""))
        ),
        smtp_from=os.environ.get("MATHLAB_SMTP_FROM", str(smtp.get("from", "MathLab <no-reply@mathlab.local>"))),
        smtp_tls_mode=os.environ.get("MATHLAB_SMTP_TLS_MODE", str(smtp.get("tls_mode", "none"))),
        smtp_socks_host=smtp_socks_host,
        smtp_socks_port=smtp_socks_port,
        default_storage_quota_bytes=int(auth.get("default_storage_quota_bytes", 536_870_912)),
        runtime_retention_days=int(runtime.get("runtime_retention_days", 7)),
        runtime_gc_poll_seconds=float(runtime.get("runtime_gc_poll_seconds", 30.0)),
        developer_active_run_override=bool(runtime.get("developer_active_run_override", True)),
        developer_max_concurrent_runs=int(runtime.get("developer_max_concurrent_runs", 10)),
        require_live_credit_pricing=os.environ.get(
            "MATHLAB_REQUIRE_LIVE_CREDIT_PRICING",
            "1" if bool(paid.get("require_live_credit_pricing", True)) else "0",
        ) == "1",
        contract_generation_reserves={
            str(key): float(value)
            for key, value in dict(paid.get("contract_generation_reserves") or {"low": 1.0, "medium": 2.0, "high": 3.0}).items()
        },
        contract_generation_estimated_minutes={
            str(key): int(value)
            for key, value in dict(paid.get("contract_generation_estimated_minutes") or {"low": 10, "medium": 15, "high": 20}).items()
        },
        contract_chat_reserves={
            str(key): float(value)
            for key, value in dict(paid.get("contract_chat_reserves") or {"low": 0.5, "medium": 1.0, "high": 2.0}).items()
        },
        expected_cost_cost_priority=float(costs.get("cost_priority", 5.0)),
        expected_cost_balanced=float(costs.get("balanced", 20.0)),
        expected_cost_capability=float(costs.get("capability_priority", 60.0)),
        model_pricing=model_pricing,
        contract_generation_max_concurrent=int(runtime.get("contract_generation_max_concurrent", 2)),
        admin_contract_generation_max_concurrent=int(runtime.get("admin_contract_generation_max_concurrent", 10)),
        user_contract_generation_max_concurrent=int(runtime.get("user_contract_generation_max_concurrent", 2)),
        admin_runner_max_concurrent_runs=int(runtime.get("admin_runner_max_concurrent_runs", 8)),
        user_runner_max_concurrent_runs=int(runtime.get("user_runner_max_concurrent_runs", 2)),
        user_max_concurrent_runs=int(runtime.get("user_max_concurrent_runs", 2)),
        compute_tool_timeout_seconds=int(runtime.get("compute_tool_timeout_seconds", 30)),
        latex_compile_timeout_seconds=int(runtime.get("latex_compile_timeout_seconds", 120)),
        public_summary_timeout_seconds=int(runtime.get("public_summary_timeout_seconds", 180)),
        execution_model_tiers=execution_model_tiers,
        execution_duration_tiers=execution_duration_tiers,
        execution_price_table=execution_price_table,
        execution_pricing_version=str(execution.get("pricing_version", "dev-v3")),
        publication_estimate_seconds=max(
            0, int(execution.get("publication_estimate_seconds", 2700))
        ),
        architecture_review_interval_seconds=int(runtime.get("architecture_review_interval_seconds", 900)),
        architecture_review_communication_threshold=int(runtime.get("architecture_review_communication_threshold", 3)),
        architecture_review_distinct_node_threshold=int(runtime.get("architecture_review_distinct_node_threshold", 2)),
        architecture_shared_bottleneck_node_threshold=int(runtime.get("architecture_shared_bottleneck_node_threshold", 2)),
        architecture_root_stagnation_seconds=int(runtime.get("architecture_root_stagnation_seconds", 1800)),
        seed_dev_users=os.environ.get("MATHLAB_SEED_DEV_USERS", "0") == "1",
    )
