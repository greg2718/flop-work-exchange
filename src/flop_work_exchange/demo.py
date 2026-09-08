from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flop_work_exchange.config import ExchangeConfig, PolicyConfig
from flop_work_exchange.exchange import WorkExchange
from flop_work_exchange.identity import create_ephemeral_party, ensure_test_identity
from flop_work_exchange.receipts import verify_receipt


def run_demo(state_dir: Path) -> dict[str, Any]:
    """Run one full paper job and write a verifiable signed receipt."""
    config = ExchangeConfig(
        state_dir=state_dir,
        payment_mode="paper",
        settlement_backend="paper",
        policy=PolicyConfig(treat_unknown_as_independent=False, allow_same_operator_deals=True),
    )
    exchange = WorkExchange(config)
    ensure_test_identity(state_dir)
    _buyer_key, buyer_did = create_ephemeral_party("buyer")
    _seller_key, seller_did = create_ephemeral_party("seller")
    # Fresh demo keys are not in the known family; classify as independent for the flywheel demo.
    exchange.credit_paper(buyer_did, "20", "demo-buyer-seed")
    job = exchange.post_job(
        buyer_did=buyer_did,
        outcome="Produce a one-line hash-locked summary of the paper settlement rules.",
        service="documentation.summary",
        budget_flop="12",
    )
    candidates = exchange.find_candidates(job.job_id)
    offer = exchange.submit_offer(
        job_id=job.job_id,
        seller_did=seller_did,
        price_flop="12",
        notes="offline stub worker offer",
    )
    plan = exchange.route_job(job.job_id)
    deal = exchange.accept_offer(offer.offer_id, relationship="independent")
    result_text = (
        "Paper settlement debits local ledgers only; payment_mode=paper; "
        "settlement_status=simulated; TCLK mode=SIMULATION_ONLY."
    )
    exchange.submit_result(job.job_id, result_text)
    exchange.verify(job.job_id)
    receipt, receipt_path = exchange.settle(job.job_id)
    payload = receipt.to_dict()
    verification = verify_receipt(payload)
    seller_profile = exchange.store.load_profile(seller_did).public_claims()
    return {
        "ok": True,
        "state_dir": str(state_dir),
        "exchange_did": exchange.exchange_did(),
        "buyer_did": buyer_did,
        "seller_did": seller_did,
        "job_id": job.job_id,
        "offer_id": offer.offer_id,
        "deal_id": deal.deal_id,
        "tclk_deal_id": deal.tclk_deal_id,
        "operator_relationship": deal.operator_relationship,
        "independent_reputation_eligible": deal.independent_reputation_eligible,
        "candidates_from_scout": [candidate.did for candidate in candidates],
        "router_qualification": plan.qualification,
        "receipt_path": str(receipt_path),
        "receipt": payload,
        "verification": verification,
        "seller_public_claims": seller_profile,
        "balances": exchange.balances(),
        "payment_mode": "paper",
        "settlement_status": "simulated",
        "settlement_execution": "DISABLED",
    }


def dumps(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)
