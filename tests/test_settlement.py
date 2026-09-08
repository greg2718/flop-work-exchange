from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from flop_work_exchange.adapters.settlement import TestnetSettlement
from flop_work_exchange.config import config_from_mapping, load_config
from flop_work_exchange.exceptions import NotLiveError, ValidationError
from tests.helpers import make_exchange


def test_testnet_settlement_raises() -> None:
    rail = TestnetSettlement()
    with pytest.raises(NotLiveError, match="not live"):
        rail.credit("did:key:z6Mk", 1, "nope")
    with pytest.raises(NotLiveError, match="faucet"):
        rail.transfer(
            debit_account="a",
            credit_account="b",
            amount_micro=1,
            reason="nope",
        )


def test_exchange_testnet_backend_cannot_credit(tmp_path: Path) -> None:
    exchange = make_exchange(tmp_path, backend="testnet")
    with pytest.raises(NotLiveError):
        exchange.credit_paper("account", "1")


def test_non_paper_payment_mode_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="payment_mode"):
        config_from_mapping(tmp_path, {"payment_mode": "live", "fees": {}})


def test_yaml_and_toml_fee_config(tmp_path: Path) -> None:
    yaml_path = tmp_path / "fees.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "payment_mode": "paper",
                "fees": {
                    "job_posting_micro": 1,
                    "orchestration_micro": 2,
                    "completion_micro": 3,
                    "bench_validation_micro": 4,
                },
            }
        ),
        encoding="utf-8",
    )
    loaded = load_config(tmp_path, yaml_path)
    assert loaded.fees.job_posting_micro == 1
    assert loaded.payment_mode == "paper"

    toml_path = tmp_path / "fees.toml"
    toml_path.write_text(
        """
payment_mode = "paper"
[fees]
job_posting_micro = 10
orchestration_micro = 20
completion_micro = 30
bench_validation_micro = 40
""",
        encoding="utf-8",
    )
    loaded_toml = load_config(tmp_path, toml_path)
    assert loaded_toml.fees.completion_micro == 30
