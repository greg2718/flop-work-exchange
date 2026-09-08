from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from flop_work_exchange.amounts import parse_micro
from flop_work_exchange.canonical import atomic_write_json
from flop_work_exchange.constants import (
    DEFAULT_SCOUT_CANDIDATE_LIMIT,
    DEFAULT_SCOUT_SQLITE_TIMEOUT_SECONDS,
    EXCHANGE_OPERATOR_GROUP,
    KNOWN_FAMILY_DIDS,
    MAX_SCOUT_CANDIDATE_LIMIT,
    SCOUT_MAX_QUERY_DB_BYTES,
    assert_isolated_state_dir,
)
from flop_work_exchange.exceptions import ValidationError

PaymentMode = Literal["paper"]
OperatorRelationship = Literal["independent", "same_operator", "related", "unknown"]
AdapterMode = Literal["stub", "local"]

DEFAULT_FEES = {
    "job_posting_micro": 100_000,
    "orchestration_micro": 200_000,
    "completion_micro": 100_000,
    "bench_validation_micro": 50_000,
    "refund_posting_fee": False,
}


def _package_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def default_fee_config_path() -> Path:
    return _package_data_dir() / "fees.json"


@dataclass(frozen=True)
class FeeSchedule:
    job_posting_micro: int = 100_000
    orchestration_micro: int = 200_000
    completion_micro: int = 100_000
    bench_validation_micro: int = 50_000
    refund_posting_fee: bool = False

    def buyer_settle_total_micro(self, worker_payout_micro: int) -> int:
        return (
            worker_payout_micro
            + self.orchestration_micro
            + self.completion_micro
            + self.bench_validation_micro
        )


def _optional_path(value: Any) -> Path | None:
    if value is None or value == "":
        return None
    return Path(str(value)).expanduser()


def _adapter_mode(value: Any, label: str) -> AdapterMode:
    mode = str(value or "stub")
    if mode not in {"stub", "local"}:
        raise ValidationError(f"{label} must be stub or local")
    return mode  # type: ignore[return-value]


def _candidate_limit(value: Any, label: str) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be an integer") from exc
    if limit < 1:
        raise ValidationError(f"{label} must be >= 1")
    return min(limit, MAX_SCOUT_CANDIDATE_LIMIT)


def _positive_float(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be a number") from exc
    if parsed <= 0:
        raise ValidationError(f"{label} must be > 0")
    return parsed


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be an integer") from exc
    if parsed <= 0:
        raise ValidationError(f"{label} must be > 0")
    return parsed


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AdapterConfig:
    """Stub vs local sibling-agent wiring. Stubs are the CI/offline default."""

    scout_mode: AdapterMode = "stub"
    bench_mode: AdapterMode = "stub"
    router_mode: AdapterMode = "stub"
    sentinel_mode: AdapterMode = "stub"
    python: str = field(default_factory=lambda: sys.executable)
    scout_repo: Path | None = None
    scout_script: Path | None = None
    scout_state_dir: Path | None = None
    scout_db: Path | None = None
    scout_projection_db: Path | None = None
    scout_evidence_jsonl: Path | None = None
    scout_sqlite_timeout_seconds: float = DEFAULT_SCOUT_SQLITE_TIMEOUT_SECONDS
    scout_max_db_bytes: int = SCOUT_MAX_QUERY_DB_BYTES
    bench_cli: str | None = None
    bench_repo: Path | None = None
    bench_allow_local_exec: bool = False
    router_repo: Path | None = None
    router_script: Path | None = None
    router_db: Path | None = None
    router_fixture: Path | None = None
    sentinel_path: Path | None = None
    timeout_seconds: float = 30.0
    scout_candidate_limit: int = DEFAULT_SCOUT_CANDIDATE_LIMIT

    def to_dict(self) -> dict[str, Any]:
        return {
            "scout_mode": self.scout_mode,
            "bench_mode": self.bench_mode,
            "router_mode": self.router_mode,
            "sentinel_mode": self.sentinel_mode,
            "python": self.python,
            "scout_repo": str(self.scout_repo) if self.scout_repo else None,
            "scout_script": str(self.scout_script) if self.scout_script else None,
            "scout_state_dir": str(self.scout_state_dir) if self.scout_state_dir else None,
            "scout_db": str(self.scout_db) if self.scout_db else None,
            "scout_projection_db": (
                str(self.scout_projection_db) if self.scout_projection_db else None
            ),
            "scout_evidence_jsonl": (
                str(self.scout_evidence_jsonl) if self.scout_evidence_jsonl else None
            ),
            "scout_sqlite_timeout_seconds": self.scout_sqlite_timeout_seconds,
            "scout_max_db_bytes": self.scout_max_db_bytes,
            "bench_cli": self.bench_cli,
            "bench_repo": str(self.bench_repo) if self.bench_repo else None,
            "bench_allow_local_exec": self.bench_allow_local_exec,
            "router_repo": str(self.router_repo) if self.router_repo else None,
            "router_script": str(self.router_script) if self.router_script else None,
            "router_db": str(self.router_db) if self.router_db else None,
            "router_fixture": str(self.router_fixture) if self.router_fixture else None,
            "sentinel_path": str(self.sentinel_path) if self.sentinel_path else None,
            "timeout_seconds": self.timeout_seconds,
            "scout_candidate_limit": self.scout_candidate_limit,
        }


@dataclass(frozen=True)
class PolicyConfig:
    allow_same_operator_deals: bool = True
    same_operator_independent_reputation: bool = False
    same_operator_fee_volume_as_independent: bool = False
    allow_self_deals: bool = False
    treat_unknown_as_independent: bool = False


@dataclass(frozen=True)
class ExchangeConfig:
    state_dir: Path
    payment_mode: PaymentMode = "paper"
    settlement_backend: Literal["paper", "testnet"] = "paper"
    settlement_execution: Literal["DISABLED"] = "DISABLED"
    tclk_mode: Literal["SIMULATION_ONLY"] = "SIMULATION_ONLY"
    operator_group: str = EXCHANGE_OPERATOR_GROUP
    known_family_dids: frozenset[str] = field(default_factory=lambda: KNOWN_FAMILY_DIDS)
    fees: FeeSchedule = field(default_factory=FeeSchedule)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    asset: str = "FLOP"
    micro_per_flop: int = 1_000_000
    adapters: AdapterConfig = field(default_factory=AdapterConfig)

    def resolved_state_dir(self) -> Path:
        return assert_isolated_state_dir(self.state_dir)


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    return value


def _fee_schedule_from_mapping(raw: dict[str, Any]) -> FeeSchedule:
    fees = _require_mapping(raw.get("fees", {}), "fees") if "fees" in raw else dict(DEFAULT_FEES)
    merged = {**DEFAULT_FEES, **fees}
    return FeeSchedule(
        job_posting_micro=parse_micro(merged["job_posting_micro"]),
        orchestration_micro=parse_micro(merged["orchestration_micro"]),
        completion_micro=parse_micro(merged["completion_micro"]),
        bench_validation_micro=parse_micro(merged["bench_validation_micro"]),
        refund_posting_fee=bool(merged.get("refund_posting_fee", False)),
    )


def _policy_from_mapping(raw: dict[str, Any]) -> PolicyConfig:
    if "policy" not in raw:
        return PolicyConfig()
    policy = _require_mapping(raw.get("policy"), "policy")
    return PolicyConfig(
        allow_same_operator_deals=bool(policy.get("allow_same_operator_deals", True)),
        same_operator_independent_reputation=bool(
            policy.get("same_operator_independent_reputation", False)
        ),
        same_operator_fee_volume_as_independent=bool(
            policy.get("same_operator_fee_volume_as_independent", False)
        ),
        allow_self_deals=bool(policy.get("allow_self_deals", False)),
        treat_unknown_as_independent=bool(policy.get("treat_unknown_as_independent", False)),
    )


def load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        loaded = json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        loaded = yaml.safe_load(text)
    elif suffix == ".toml":
        import tomllib

        loaded = tomllib.loads(text)
    else:
        raise ValidationError(f"unsupported config format: {suffix}")
    return _require_mapping(loaded, "config")


def config_from_mapping(state_dir: Path, raw: dict[str, Any]) -> ExchangeConfig:
    payment_mode = raw.get("payment_mode", "paper")
    if payment_mode != "paper":
        raise ValidationError('payment_mode must be "paper"; live rails are not available')
    backend = raw.get("settlement_backend", "paper")
    if backend not in {"paper", "testnet"}:
        raise ValidationError('settlement_backend must be "paper" or "testnet"')
    family = raw.get("known_family_dids")
    known = KNOWN_FAMILY_DIDS
    if family is not None:
        if not isinstance(family, list) or not all(isinstance(item, str) for item in family):
            raise ValidationError("known_family_dids must be a list of DID strings")
        known = frozenset(family)
    return ExchangeConfig(
        state_dir=state_dir,
        payment_mode="paper",
        settlement_backend=backend,
        fees=_fee_schedule_from_mapping(raw),
        policy=_policy_from_mapping(raw),
        known_family_dids=known,
        operator_group=str(raw.get("operator_group", EXCHANGE_OPERATOR_GROUP)),
        asset=str(raw.get("asset", "FLOP")),
        adapters=overlay_adapter_env(adapter_config_from_mapping(raw.get("adapters"))),
    )


def load_config(state_dir: Path, config_path: Path | None = None) -> ExchangeConfig:
    path = config_path or default_fee_config_path()
    return config_from_mapping(state_dir, load_mapping(path))


def adapter_config_from_mapping(raw: Any | None) -> AdapterConfig:
    if raw is None:
        return AdapterConfig()
    mapping = _require_mapping(raw, "adapters")
    timeout_raw = mapping.get("timeout_seconds", 30.0)
    try:
        timeout = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("adapters.timeout_seconds must be a number") from exc
    sqlite_timeout = mapping.get(
        "scout_sqlite_timeout_seconds", DEFAULT_SCOUT_SQLITE_TIMEOUT_SECONDS
    )
    max_db_bytes = mapping.get("scout_max_db_bytes", SCOUT_MAX_QUERY_DB_BYTES)
    return AdapterConfig(
        scout_mode=_adapter_mode(mapping.get("scout_mode", "stub"), "scout_mode"),
        bench_mode=_adapter_mode(mapping.get("bench_mode", "stub"), "bench_mode"),
        router_mode=_adapter_mode(mapping.get("router_mode", "stub"), "router_mode"),
        sentinel_mode=_adapter_mode(mapping.get("sentinel_mode", "stub"), "sentinel_mode"),
        python=str(mapping.get("python") or sys.executable),
        scout_repo=_optional_path(mapping.get("scout_repo")),
        scout_script=_optional_path(mapping.get("scout_script")),
        scout_state_dir=_optional_path(mapping.get("scout_state_dir")),
        scout_db=_optional_path(mapping.get("scout_db")),
        scout_projection_db=_optional_path(mapping.get("scout_projection_db")),
        scout_evidence_jsonl=_optional_path(mapping.get("scout_evidence_jsonl")),
        scout_sqlite_timeout_seconds=_positive_float(
            sqlite_timeout, "adapters.scout_sqlite_timeout_seconds"
        ),
        scout_max_db_bytes=_positive_int(max_db_bytes, "adapters.scout_max_db_bytes"),
        bench_cli=str(mapping["bench_cli"]) if mapping.get("bench_cli") else None,
        bench_repo=_optional_path(mapping.get("bench_repo")),
        bench_allow_local_exec=bool(mapping.get("bench_allow_local_exec", False)),
        router_repo=_optional_path(mapping.get("router_repo")),
        router_script=_optional_path(mapping.get("router_script")),
        router_db=_optional_path(mapping.get("router_db")),
        router_fixture=_optional_path(mapping.get("router_fixture")),
        sentinel_path=_optional_path(mapping.get("sentinel_path")),
        timeout_seconds=timeout,
        scout_candidate_limit=_candidate_limit(
            mapping.get("scout_candidate_limit", DEFAULT_SCOUT_CANDIDATE_LIMIT),
            "adapters.scout_candidate_limit",
        ),
    )


def adapter_config_from_env() -> AdapterConfig:
    return overlay_adapter_env(AdapterConfig())


def load_adapter_config(config_path: Path | None = None) -> AdapterConfig:
    """Load AdapterConfig from the same --config mapping as ExchangeConfig.

    File/YAML adapter settings apply first; ``FLOP_WX_*`` env vars still win
    when set. Omitting ``config_path`` is env-only (stubs remain the default).
    """
    if config_path is None:
        return adapter_config_from_env()
    raw = load_mapping(config_path)
    return overlay_adapter_env(adapter_config_from_mapping(raw.get("adapters")))


def overlay_adapter_env(base: AdapterConfig) -> AdapterConfig:
    """Environment variables override file/config defaults. Stubs remain default."""
    updates: dict[str, Any] = {}
    for field_name, env_name in (
        ("scout_mode", "FLOP_WX_SCOUT_MODE"),
        ("bench_mode", "FLOP_WX_BENCH_MODE"),
        ("router_mode", "FLOP_WX_ROUTER_MODE"),
        ("sentinel_mode", "FLOP_WX_SENTINEL_MODE"),
    ):
        raw = os.environ.get(env_name)
        if raw:
            updates[field_name] = _adapter_mode(raw, env_name)
    if os.environ.get("FLOP_WX_PYTHON"):
        updates["python"] = os.environ["FLOP_WX_PYTHON"]
    path_envs = {
        "scout_repo": "FLOP_WX_SCOUT_REPO",
        "scout_script": "FLOP_WX_SCOUT_SCRIPT",
        "scout_state_dir": "FLOP_WX_SCOUT_STATE_DIR",
        "scout_db": "FLOP_WX_SCOUT_DB",
        "scout_projection_db": "FLOP_WX_SCOUT_PROJECTION_DB",
        "scout_evidence_jsonl": "FLOP_WX_SCOUT_EVIDENCE_JSONL",
        "bench_repo": "FLOP_WX_BENCH_REPO",
        "router_repo": "FLOP_WX_ROUTER_REPO",
        "router_script": "FLOP_WX_ROUTER_SCRIPT",
        "router_db": "FLOP_WX_ROUTER_DB",
        "router_fixture": "FLOP_WX_ROUTER_FIXTURE",
        "sentinel_path": "FLOP_WX_SENTINEL_PATH",
    }
    for field_name, env_name in path_envs.items():
        raw = os.environ.get(env_name)
        if raw:
            updates[field_name] = _optional_path(raw)
    if not updates.get("scout_state_dir") and os.environ.get("FLOP_SCOUT_STATE_DIR"):
        updates["scout_state_dir"] = _optional_path(os.environ["FLOP_SCOUT_STATE_DIR"])
    if os.environ.get("FLOP_WX_BENCH_CLI"):
        updates["bench_cli"] = os.environ["FLOP_WX_BENCH_CLI"]
    allow_exec = _env_flag("FLOP_WX_BENCH_ALLOW_LOCAL_EXEC")
    if allow_exec is not None:
        updates["bench_allow_local_exec"] = allow_exec
    if os.environ.get("FLOP_WX_ADAPTER_TIMEOUT"):
        try:
            updates["timeout_seconds"] = float(os.environ["FLOP_WX_ADAPTER_TIMEOUT"])
        except ValueError as exc:
            raise ValidationError("FLOP_WX_ADAPTER_TIMEOUT must be a number") from exc
    if os.environ.get("FLOP_WX_SCOUT_SQLITE_TIMEOUT"):
        updates["scout_sqlite_timeout_seconds"] = _positive_float(
            os.environ["FLOP_WX_SCOUT_SQLITE_TIMEOUT"],
            "FLOP_WX_SCOUT_SQLITE_TIMEOUT",
        )
    if os.environ.get("FLOP_WX_SCOUT_MAX_DB_BYTES"):
        updates["scout_max_db_bytes"] = _positive_int(
            os.environ["FLOP_WX_SCOUT_MAX_DB_BYTES"],
            "FLOP_WX_SCOUT_MAX_DB_BYTES",
        )
    if os.environ.get("FLOP_WX_SCOUT_CANDIDATE_LIMIT"):
        updates["scout_candidate_limit"] = _candidate_limit(
            os.environ["FLOP_WX_SCOUT_CANDIDATE_LIMIT"],
            "FLOP_WX_SCOUT_CANDIDATE_LIMIT",
        )
    return replace(base, **updates) if updates else base


def write_resolved_config(state_dir: Path, config: ExchangeConfig) -> None:
    payload = {
        "payment_mode": config.payment_mode,
        "settlement_backend": config.settlement_backend,
        "settlement_execution": config.settlement_execution,
        "tclk_mode": config.tclk_mode,
        "operator_group": config.operator_group,
        "asset": config.asset,
        "micro_per_flop": config.micro_per_flop,
        "known_family_dids": sorted(config.known_family_dids),
        "fees": {
            "job_posting_micro": config.fees.job_posting_micro,
            "orchestration_micro": config.fees.orchestration_micro,
            "completion_micro": config.fees.completion_micro,
            "bench_validation_micro": config.fees.bench_validation_micro,
            "refund_posting_fee": config.fees.refund_posting_fee,
        },
        "policy": {
            "allow_same_operator_deals": config.policy.allow_same_operator_deals,
            "same_operator_independent_reputation": (
                config.policy.same_operator_independent_reputation
            ),
            "same_operator_fee_volume_as_independent": (
                config.policy.same_operator_fee_volume_as_independent
            ),
            "allow_self_deals": config.policy.allow_self_deals,
            "treat_unknown_as_independent": config.policy.treat_unknown_as_independent,
        },
        "adapters": config.adapters.to_dict(),
    }
    atomic_write_json(state_dir / "config.json", payload)
