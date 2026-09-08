from __future__ import annotations

from pathlib import Path

import pytest

from flop_work_exchange.amounts import micro_to_flop_string, parse_flop_to_micro
from flop_work_exchange.config import FeeSchedule
from flop_work_exchange.exceptions import InsufficientFundsError, ValidationError
from flop_work_exchange.exchange import BENCH_FEE_ACCOUNT, EXCHANGE_FEE_ACCOUNT
from tests.helpers import fresh_dids, make_exchange


def test_parse_flop_exact_micro_no_float() -> None:
    assert parse_flop_to_micro("12") == 12_000_000
    assert parse_flop_to_micro("12.5") == 12_500_000
    assert parse_flop_to_micro("0.000001") == 1
    assert micro_to_flop_string(12_000_000) == "12"
    assert micro_to_flop_string(12_500_000) == "12.5"
    with pytest.raises(ValidationError):
        parse_flop_to_micro("12.1234567")
    with pytest.raises(ValidationError):
        parse_flop_to_micro("1e2")


def test_fee_debits_and_credits(tmp_path: Path) -> None:
    fees = FeeSchedule(
        job_posting_micro=100_000,
        orchestration_micro=200_000,
        completion_micro=100_000,
        bench_validation_micro=50_000,
    )
    exchange = make_exchange(tmp_path, fees=fees)
    buyer, seller = fresh_dids()
    exchange.credit_paper(buyer, "20")
    job = exchange.post_job(
        buyer_did=buyer,
        outcome="Summarize paper settlement.",
        service="docs",
        budget_flop="12",
    )
    assert exchange.store.paper_balance(buyer) == 20_000_000 - 100_000
    assert exchange.store.paper_balance(EXCHANGE_FEE_ACCOUNT) == 100_000
    offer = exchange.submit_offer(job_id=job.job_id, seller_did=seller, price_flop="12")
    exchange.accept_offer(offer.offer_id, relationship="independent")
    exchange.submit_result(job.job_id, "done")
    exchange.verify(job.job_id)
    exchange.settle(job.job_id)
    assert exchange.store.paper_balance(seller) == 12_000_000
    assert exchange.store.paper_balance(EXCHANGE_FEE_ACCOUNT) == 100_000 + 200_000 + 100_000
    assert exchange.store.paper_balance(BENCH_FEE_ACCOUNT) == 50_000
    assert exchange.store.paper_balance(buyer) == 20_000_000 - 100_000 - 12_350_000


def test_insufficient_paper_balance(tmp_path: Path) -> None:
    exchange = make_exchange(tmp_path)
    buyer, _seller = fresh_dids()
    with pytest.raises(InsufficientFundsError):
        exchange.post_job(
            buyer_did=buyer,
            outcome="No seed balance.",
            service="task",
            budget_flop="1",
        )


def test_zero_fee_schedule(tmp_path: Path) -> None:
    fees = FeeSchedule(
        job_posting_micro=0,
        orchestration_micro=0,
        completion_micro=0,
        bench_validation_micro=0,
    )
    exchange = make_exchange(tmp_path, fees=fees)
    buyer, seller = fresh_dids()
    exchange.credit_paper(buyer, "12")
    job = exchange.post_job(buyer_did=buyer, outcome="Zero fees", service="task", budget_flop="12")
    offer = exchange.submit_offer(job_id=job.job_id, seller_did=seller, price_flop="12")
    exchange.accept_offer(offer.offer_id, relationship="independent")
    exchange.submit_result(job.job_id, "ok")
    exchange.verify(job.job_id)
    exchange.settle(job.job_id)
    assert exchange.store.paper_balance(buyer) == 0
    assert exchange.store.paper_balance(seller) == 12_000_000
    assert exchange.store.paper_balance(EXCHANGE_FEE_ACCOUNT) == 0
