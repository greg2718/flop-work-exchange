from __future__ import annotations

from pathlib import Path

from flop_work_exchange.config import ExchangeConfig, FeeSchedule, PolicyConfig
from flop_work_exchange.exchange import WorkExchange
from flop_work_exchange.identity import create_ephemeral_party, ensure_test_identity


def make_exchange(
    tmp_path: Path,
    *,
    fees: FeeSchedule | None = None,
    policy: PolicyConfig | None = None,
    backend: str = "paper",
) -> WorkExchange:
    config = ExchangeConfig(
        state_dir=tmp_path,
        settlement_backend=backend,  # type: ignore[arg-type]
        fees=fees or FeeSchedule(),
        policy=policy or PolicyConfig(),
    )
    exchange = WorkExchange(config)
    ensure_test_identity(tmp_path)
    return exchange


def fresh_dids() -> tuple[str, str]:
    _, buyer = create_ephemeral_party("buyer")
    _, seller = create_ephemeral_party("seller")
    return buyer, seller
