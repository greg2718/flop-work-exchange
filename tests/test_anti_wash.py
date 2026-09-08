from __future__ import annotations

from pathlib import Path

import pytest

from flop_work_exchange.constants import BENCH_DID, ROUTER_DID, SCOUT_DID
from flop_work_exchange.exceptions import PolicyError, SafetyError
from flop_work_exchange.policy import classify_operator_relationship
from tests.helpers import fresh_dids, make_exchange


def test_family_dids_are_same_operator() -> None:
    assert classify_operator_relationship(SCOUT_DID, BENCH_DID) == "same_operator"
    assert classify_operator_relationship(SCOUT_DID, ROUTER_DID) == "same_operator"
    assert classify_operator_relationship(SCOUT_DID, SCOUT_DID) == "same_operator"


def test_family_plus_outsider_is_related() -> None:
    _, outsider = fresh_dids()
    assert classify_operator_relationship(SCOUT_DID, outsider) == "related"


def test_same_operator_deal_does_not_mint_independent_reputation(tmp_path: Path) -> None:
    exchange = make_exchange(tmp_path)
    exchange.credit_paper(SCOUT_DID, "20")
    job = exchange.post_job(
        buyer_did=SCOUT_DID,
        outcome="Family demo job.",
        service="demo",
        budget_flop="12",
    )
    offer = exchange.submit_offer(job_id=job.job_id, seller_did=BENCH_DID, price_flop="12")
    deal = exchange.accept_offer(offer.offer_id)
    assert deal.operator_relationship == "same_operator"
    assert deal.independent_reputation_eligible is False
    assert deal.independent_fee_volume_eligible is False
    exchange.submit_result(job.job_id, "family result")
    exchange.verify(job.job_id)
    exchange.settle(job.job_id)
    seller = exchange.store.load_profile(BENCH_DID)
    buyer = exchange.store.load_profile(SCOUT_DID)
    assert seller.independent_completed_jobs == 0
    assert seller.independent_fee_volume_micro == 0
    assert seller.same_operator_completed_jobs == 1
    assert seller.same_operator_fee_volume_micro == 12_000_000
    claims = seller.public_claims()
    assert claims["independent_completed_jobs"] == 0
    assert claims["independent_fee_volume_micro"] == 0
    assert buyer.independent_completed_jobs == 0


def test_cannot_relabel_family_as_independent(tmp_path: Path) -> None:
    exchange = make_exchange(tmp_path)
    exchange.credit_paper(SCOUT_DID, "20")
    job = exchange.post_job(
        buyer_did=SCOUT_DID, outcome="Relabel attempt", service="demo", budget_flop="1"
    )
    offer = exchange.submit_offer(job_id=job.job_id, seller_did=BENCH_DID, price_flop="1")
    with pytest.raises(SafetyError, match="same-operator"):
        exchange.accept_offer(offer.offer_id, relationship="independent")


def test_self_deal_blocked_by_default(tmp_path: Path) -> None:
    exchange = make_exchange(tmp_path)
    buyer, _seller = fresh_dids()
    exchange.credit_paper(buyer, "20")
    job = exchange.post_job(buyer_did=buyer, outcome="Self deal", service="demo", budget_flop="1")
    offer = exchange.submit_offer(job_id=job.job_id, seller_did=buyer, price_flop="1")
    with pytest.raises(PolicyError, match="self-deals"):
        exchange.accept_offer(offer.offer_id)


def test_circular_counterparties_flag_wash_risk(tmp_path: Path) -> None:
    exchange = make_exchange(tmp_path)
    a, b = fresh_dids()
    exchange.credit_paper(a, "40")
    exchange.credit_paper(b, "40")
    job1 = exchange.post_job(buyer_did=a, outcome="A buys B", service="demo", budget_flop="1")
    offer1 = exchange.submit_offer(job_id=job1.job_id, seller_did=b, price_flop="1")
    exchange.accept_offer(offer1.offer_id, relationship="independent")
    exchange.submit_result(job1.job_id, "one")
    exchange.verify(job1.job_id)
    exchange.settle(job1.job_id)

    job2 = exchange.post_job(buyer_did=b, outcome="B buys A", service="demo", budget_flop="1")
    offer2 = exchange.submit_offer(job_id=job2.job_id, seller_did=a, price_flop="1")
    deal2 = exchange.accept_offer(offer2.offer_id, relationship="independent")
    assert deal2.wash_risk is True
    assert deal2.independent_reputation_eligible is False
    assert deal2.independent_fee_volume_eligible is False
    exchange.submit_result(job2.job_id, "two")
    exchange.verify(job2.job_id)
    exchange.settle(job2.job_id)
    profile_a = exchange.store.load_profile(a)
    assert profile_a.wash_flags >= 1
    # First job counted independent; reverse pair did not.
    assert profile_a.independent_completed_jobs == 1
