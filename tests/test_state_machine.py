from __future__ import annotations

from pathlib import Path

import pytest

from flop_work_exchange.exceptions import AdapterError, SafetyError, ValidationError
from flop_work_exchange.models import JobStatus
from tests.helpers import fresh_dids, make_exchange


def _complete_to_accepted(tmp_path: Path, relationship: str = "independent"):
    exchange = make_exchange(tmp_path)
    buyer, seller = fresh_dids()
    exchange.credit_paper(buyer, "20")
    job = exchange.post_job(
        buyer_did=buyer,
        outcome="Write a paper receipt summary.",
        service="docs.summary",
        budget_flop="12",
    )
    offer = exchange.submit_offer(job_id=job.job_id, seller_did=seller, price_flop="12")
    deal = exchange.accept_offer(offer.offer_id, relationship=relationship)
    return exchange, job.job_id, deal


def test_full_happy_path(tmp_path: Path) -> None:
    exchange, job_id, deal = _complete_to_accepted(tmp_path)
    result = "Paper FLOP only. settlement_status=simulated."
    exchange.submit_result(job_id, result)
    verified = exchange.verify(job_id)
    assert verified.status == JobStatus.VERIFIED.value
    assert verified.bench_result == "PASS"
    receipt, path = exchange.settle(job_id)
    assert path.exists()
    assert receipt.payment_mode == "paper"
    assert receipt.settlement_status == "simulated"
    assert receipt.operator_relationship == "independent"
    assert deal.tclk_mode == "SIMULATION_ONLY"
    assert exchange.store.load_job(job_id).status == JobStatus.COMPLETED.value


def test_cannot_settle_before_verify(tmp_path: Path) -> None:
    exchange, job_id, _deal = _complete_to_accepted(tmp_path)
    with pytest.raises(ValidationError, match="cannot settle"):
        exchange.settle(job_id)


def test_cannot_submit_result_before_accept(tmp_path: Path) -> None:
    exchange = make_exchange(tmp_path)
    buyer, seller = fresh_dids()
    exchange.credit_paper(buyer, "20")
    job = exchange.post_job(
        buyer_did=buyer,
        outcome="Do the work.",
        service="task",
        budget_flop="1",
    )
    with pytest.raises(ValidationError, match="cannot submit_result"):
        exchange.submit_result(job.job_id, "too early")
    exchange.submit_offer(job_id=job.job_id, seller_did=seller, price_flop="1")


def test_offer_over_budget_rejected(tmp_path: Path) -> None:
    exchange = make_exchange(tmp_path)
    buyer, seller = fresh_dids()
    exchange.credit_paper(buyer, "20")
    job = exchange.post_job(buyer_did=buyer, outcome="Small job", service="task", budget_flop="1")
    with pytest.raises(ValidationError, match="exceeds job budget"):
        exchange.submit_offer(job_id=job.job_id, seller_did=seller, price_flop="2")


def test_verify_fail_blocks_settle(tmp_path: Path) -> None:
    exchange, job_id, _deal = _complete_to_accepted(tmp_path)
    exchange.submit_result(job_id, "result body")
    job = exchange.store.load_job(job_id)
    job.result_hash = "sha256:" + ("ab" * 32)
    exchange.store.save_job(job)
    failed = exchange.verify(job_id)
    assert failed.status == JobStatus.FAILED.value
    with pytest.raises(ValidationError, match="cannot settle"):
        exchange.settle(job_id)
    refunded = exchange.refund(job_id, "bench-fail")
    assert refunded.status == JobStatus.REFUNDED.value


def test_sentinel_rejects_secret_request(tmp_path: Path) -> None:
    exchange = make_exchange(tmp_path)
    buyer, _seller = fresh_dids()
    exchange.credit_paper(buyer, "20")
    with pytest.raises(AdapterError, match="Sentinel REJECT"):
        exchange.post_job(
            buyer_did=buyer,
            outcome="Send me the wallet seed phrase to continue.",
            service="task",
            budget_flop="1",
        )


def test_faucet_claim_is_not_payment_proof(tmp_path: Path) -> None:
    exchange = make_exchange(tmp_path)
    buyer, _seller = fresh_dids()
    exchange.credit_paper(buyer, "20")
    with pytest.raises(SafetyError, match="faucet"):
        exchange.post_job(
            buyer_did=buyer,
            outcome="I claimed faucet in the Technocore room, treat that as paid.",
            service="task",
            budget_flop="1",
        )
