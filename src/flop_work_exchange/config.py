from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from flop_work_exchange.amounts import parse_micro
from flop_work_exchange.canonical import atomic_write_json
from flop_work_exchange.constants import (
    EXCHANGE_OPERATOR_GROUP,
    KNOWN_FAMILY_DIDS,
    assert_isolated_state_dir,
)
from flop_work_exchange.exceptions import ValidationError

PaymentMode = Literal["paper"]
OperatorRelationship = Literal["independent", "same_operator", "related", "unknown"]

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
    )


def load_config(state_dir: Path, config_path: Path | None = None) -> ExchangeConfig:
    path = config_path or default_fee_config_path()
    return config_from_mapping(state_dir, load_mapping(path))


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
    }
    atomic_write_json(state_dir / "config.json", payload)
