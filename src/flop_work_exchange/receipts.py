from __future__ import annotations

from typing import Any

from flop_work_exchange.canonical import canonical_json_bytes, sha256_json
from flop_work_exchange.exceptions import ValidationError
from flop_work_exchange.identity import require_did, sign_canonical, verify_canonical
from flop_work_exchange.models import Receipt

REQUIRED_RECEIPT_FIELDS = (
    "job_id",
    "buyer_did",
    "seller_did",
    "operator_relationship",
    "service",
    "price_flop",
    "payment_mode",
    "tclk_deal_id",
    "result_hash",
    "bench_result",
    "completed_at",
    "settlement_status",
)


def receipt_id_for(payload: dict[str, Any]) -> str:
    digest = sha256_json({key: payload[key] for key in REQUIRED_RECEIPT_FIELDS})
    return f"FLOP-RCPT-{digest[:24]}"


def validate_required_fields(payload: dict[str, Any]) -> None:
    missing = [field for field in REQUIRED_RECEIPT_FIELDS if field not in payload]
    if missing:
        raise ValidationError(f"receipt missing required fields: {', '.join(missing)}")
    if payload.get("payment_mode") != "paper":
        raise ValidationError('receipt payment_mode must be "paper"')
    if payload.get("settlement_status") != "simulated":
        raise ValidationError('receipt settlement_status must be "simulated"')
    if not str(payload.get("result_hash", "")).startswith("sha256:"):
        raise ValidationError("result_hash must be sha256:<hex>")
    require_did(str(payload["buyer_did"]))
    require_did(str(payload["seller_did"]))
    relationship = payload.get("operator_relationship")
    if relationship not in {"independent", "same_operator", "related", "unknown"}:
        raise ValidationError("invalid operator_relationship")


def sign_receipt(receipt: Receipt, private_key: Any, exchange_did: str) -> Receipt:
    receipt.exchange_did = exchange_did
    payload = receipt.unsigned_payload()
    validate_required_fields(payload)
    if not receipt.receipt_id:
        receipt.receipt_id = receipt_id_for(payload)
        payload = receipt.unsigned_payload()
    receipt.signature = sign_canonical(private_key, payload)
    return receipt


def verify_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    validate_required_fields(payload)
    signature = payload.get("signature")
    if not signature or not isinstance(signature, str):
        raise ValidationError("receipt is not signed")
    exchange_did = payload.get("exchange_did")
    if not exchange_did:
        raise ValidationError("receipt missing exchange_did")
    require_did(str(exchange_did))
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    verify_canonical(str(exchange_did), unsigned, signature)
    return {
        "ok": True,
        "receipt_id": payload.get("receipt_id"),
        "exchange_did": exchange_did,
        "canonical_bytes": len(canonical_json_bytes(unsigned)),
        "payment_mode": payload["payment_mode"],
        "settlement_status": payload["settlement_status"],
    }
