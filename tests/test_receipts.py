from __future__ import annotations

from pathlib import Path

import pytest

from flop_work_exchange.exceptions import ValidationError
from flop_work_exchange.identity import (
    create_ephemeral_party,
    generate_key,
    public_did,
    sign_canonical,
)
from flop_work_exchange.models import Receipt
from flop_work_exchange.receipts import sign_receipt, verify_receipt
from tests.helpers import fresh_dids, make_exchange


def test_sign_and_verify_receipt_roundtrip() -> None:
    key = generate_key()
    did = public_did(key)
    _, buyer = create_ephemeral_party("b")
    _, seller = create_ephemeral_party("s")
    receipt = Receipt(
        job_id="FLOP-JOB-demo",
        buyer_did=buyer,
        seller_did=seller,
        operator_relationship="independent",
        service="docs",
        price_flop="12",
        payment_mode="paper",
        tclk_deal_id="tclk-paper-abc",
        result_hash="sha256:" + ("ab" * 32),
        bench_result="PASS",
        completed_at="2026-09-08T00:00:00Z",
        settlement_status="simulated",
        price_micro=12_000_000,
    )
    sign_receipt(receipt, key, did)
    payload = receipt.to_dict()
    assert payload["signature"]
    assert verify_receipt(payload)["ok"] is True


def test_tampered_receipt_fails_verify() -> None:
    key = generate_key()
    did = public_did(key)
    _, buyer = create_ephemeral_party("b")
    _, seller = create_ephemeral_party("s")
    receipt = Receipt(
        job_id="FLOP-JOB-demo",
        buyer_did=buyer,
        seller_did=seller,
        operator_relationship="independent",
        service="docs",
        price_flop="12",
        payment_mode="paper",
        tclk_deal_id="tclk-paper-abc",
        result_hash="sha256:" + ("ab" * 32),
        bench_result="PASS",
        completed_at="2026-09-08T00:00:00Z",
        settlement_status="simulated",
    )
    sign_receipt(receipt, key, did)
    payload = receipt.to_dict()
    payload["price_flop"] = "999"
    with pytest.raises(ValidationError, match="signature verification failed"):
        verify_receipt(payload)


def test_wrong_key_cannot_verify() -> None:
    key = generate_key()
    other = generate_key()
    did = public_did(key)
    other_did = public_did(other)
    _, buyer = create_ephemeral_party("b")
    _, seller = create_ephemeral_party("s")
    receipt = Receipt(
        job_id="FLOP-JOB-demo",
        buyer_did=buyer,
        seller_did=seller,
        operator_relationship="independent",
        service="docs",
        price_flop="12",
        payment_mode="paper",
        tclk_deal_id="tclk-paper-abc",
        result_hash="sha256:" + ("ab" * 32),
        bench_result="PASS",
        completed_at="2026-09-08T00:00:00Z",
        settlement_status="simulated",
    )
    sign_receipt(receipt, key, did)
    payload = receipt.to_dict()
    payload["exchange_did"] = other_did
    with pytest.raises(ValidationError):
        verify_receipt(payload)
    # Forged signature from a different key under the original DID also fails.
    unsigned = {k: v for k, v in payload.items() if k != "signature"}
    unsigned["exchange_did"] = did
    payload["exchange_did"] = did
    payload["signature"] = sign_canonical(other, unsigned)
    with pytest.raises(ValidationError, match="signature verification failed"):
        verify_receipt(payload)


def test_live_payment_mode_rejected_on_receipt() -> None:
    key = generate_key()
    did = public_did(key)
    _, buyer = create_ephemeral_party("b")
    _, seller = create_ephemeral_party("s")
    receipt = Receipt(
        job_id="FLOP-JOB-demo",
        buyer_did=buyer,
        seller_did=seller,
        operator_relationship="independent",
        service="docs",
        price_flop="12",
        payment_mode="live",
        tclk_deal_id="tclk-paper-abc",
        result_hash="sha256:" + ("ab" * 32),
        bench_result="PASS",
        completed_at="2026-09-08T00:00:00Z",
        settlement_status="simulated",
    )
    with pytest.raises(ValidationError, match="payment_mode"):
        sign_receipt(receipt, key, did)


def test_demo_receipt_file_verifies(tmp_path: Path) -> None:
    exchange = make_exchange(tmp_path)
    buyer, seller = fresh_dids()
    exchange.credit_paper(buyer, "20")
    job = exchange.post_job(
        buyer_did=buyer, outcome="Hash-lock a summary.", service="docs", budget_flop="12"
    )
    offer = exchange.submit_offer(job_id=job.job_id, seller_did=seller, price_flop="12")
    exchange.accept_offer(offer.offer_id, relationship="independent")
    exchange.submit_result(job.job_id, "summary")
    exchange.verify(job.job_id)
    receipt, path = exchange.settle(job.job_id)
    shown = exchange.show_receipt(job.job_id)
    assert shown["verification"]["ok"] is True
    assert path.read_text(encoding="utf-8")
    assert receipt.job_id == job.job_id
