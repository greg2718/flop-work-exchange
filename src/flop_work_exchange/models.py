from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self, TypeVar

from flop_work_exchange.config import OperatorRelationship
from flop_work_exchange.identity import is_valid_ed25519_did


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class JobStatus(StrEnum):
    POSTED = "POSTED"
    OFFERED = "OFFERED"
    ACCEPTED = "ACCEPTED"
    RESULT_SUBMITTED = "RESULT_SUBMITTED"
    VERIFIED = "VERIFIED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    REJECTED = "REJECTED"


T = TypeVar("T")


def _from_mapping(cls: type[T], raw: dict[str, Any]) -> T:
    allowed = {item.name for item in fields(cls)}  # type: ignore[arg-type]
    payload = {key: value for key, value in raw.items() if key in allowed}
    return cls(**payload)


@dataclass
class Job:
    job_id: str
    buyer_did: str
    outcome: str
    service: str
    budget_micro: int
    status: str = JobStatus.POSTED.value
    payment_mode: str = "paper"
    created_at: str = field(default_factory=now_iso)
    accepted_offer_id: str | None = None
    deal_id: str | None = None
    result_text: str | None = None
    result_hash: str | None = None
    bench_result: str | None = None
    receipt_id: str | None = None
    sentinel_status: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        job = _from_mapping(cls, raw)
        job.notes = list(raw.get("notes") or [])
        return job


@dataclass
class Offer:
    offer_id: str
    job_id: str
    seller_did: str
    price_micro: int
    created_at: str = field(default_factory=now_iso)
    status: str = "OPEN"
    sentinel_status: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return _from_mapping(cls, raw)


@dataclass
class Deal:
    deal_id: str
    job_id: str
    offer_id: str
    buyer_did: str
    seller_did: str
    operator_relationship: OperatorRelationship
    price_micro: int
    payment_mode: str = "paper"
    tclk_deal_id: str | None = None
    tclk_mode: str = "SIMULATION_ONLY"
    settlement_execution: str = "DISABLED"
    created_at: str = field(default_factory=now_iso)
    independent_reputation_eligible: bool = False
    independent_fee_volume_eligible: bool = False
    wash_risk: bool = False
    work_route: dict[str, Any] = field(default_factory=dict)
    settlement_plan: dict[str, Any] = field(default_factory=dict)
    verification_plan: dict[str, Any] = field(default_factory=dict)
    security_policy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        deal = _from_mapping(cls, raw)
        deal.work_route = dict(raw.get("work_route") or {})
        deal.settlement_plan = dict(raw.get("settlement_plan") or {})
        deal.verification_plan = dict(raw.get("verification_plan") or {})
        deal.security_policy = dict(raw.get("security_policy") or {})
        return deal


@dataclass
class Receipt:
    job_id: str
    buyer_did: str
    seller_did: str
    operator_relationship: OperatorRelationship
    service: str
    price_flop: str
    payment_mode: str
    tclk_deal_id: str
    result_hash: str
    bench_result: str
    completed_at: str
    settlement_status: str
    receipt_id: str = ""
    exchange_did: str = ""
    schema: str = "flop-work-exchange.receipt.v0.1"
    price_micro: int = 0
    independent_reputation_eligible: bool = False
    independent_fee_volume_eligible: bool = False
    signature: str | None = None

    def unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature", None)
        return payload

    def required_fields(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "buyer_did": self.buyer_did,
            "seller_did": self.seller_did,
            "operator_relationship": self.operator_relationship,
            "service": self.service,
            "price_flop": self.price_flop,
            "payment_mode": self.payment_mode,
            "tclk_deal_id": self.tclk_deal_id,
            "result_hash": self.result_hash,
            "bench_result": self.bench_result,
            "completed_at": self.completed_at,
            "settlement_status": self.settlement_status,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return _from_mapping(cls, raw)


@dataclass
class EvidenceProfile:
    did: str
    completed_jobs: int = 0
    independent_completed_jobs: int = 0
    same_operator_completed_jobs: int = 0
    related_completed_jobs: int = 0
    unknown_completed_jobs: int = 0
    independent_fee_volume_micro: int = 0
    same_operator_fee_volume_micro: int = 0
    related_fee_volume_micro: int = 0
    unknown_fee_volume_micro: int = 0
    bench_pass_count: int = 0
    last_receipt_id: str | None = None
    last_job_id: str | None = None
    last_result_hash: str | None = None
    wash_flags: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_claims(self) -> dict[str, Any]:
        """Public claims never treat same-operator volume as independent."""
        return {
            "did": self.did,
            "completed_jobs": self.completed_jobs,
            "independent_completed_jobs": self.independent_completed_jobs,
            "independent_fee_volume_micro": self.independent_fee_volume_micro,
            "non_independent_completed_jobs": (
                self.same_operator_completed_jobs
                + self.related_completed_jobs
                + self.unknown_completed_jobs
            ),
            "bench_pass_count": self.bench_pass_count,
            "note": (
                "independent_* counters exclude same_operator, related, and unknown deals. "
                "Same-operator family activity is not independent reputation or fee volume."
            ),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return _from_mapping(cls, raw)

    def record_completion(
        self,
        *,
        relationship: OperatorRelationship,
        fee_volume_micro: int,
        receipt_id: str,
        job_id: str,
        result_hash: str,
        bench_result: str,
        independent_reputation_eligible: bool,
        independent_fee_volume_eligible: bool,
        wash_risk: bool,
    ) -> None:
        self.completed_jobs += 1
        self.last_receipt_id = receipt_id
        self.last_job_id = job_id
        self.last_result_hash = result_hash
        if bench_result == "PASS":
            self.bench_pass_count += 1
        if wash_risk:
            self.wash_flags += 1
        if relationship == "same_operator":
            self.same_operator_completed_jobs += 1
            self.same_operator_fee_volume_micro += fee_volume_micro
        elif relationship == "related":
            self.related_completed_jobs += 1
            self.related_fee_volume_micro += fee_volume_micro
        elif relationship == "unknown":
            self.unknown_completed_jobs += 1
            self.unknown_fee_volume_micro += fee_volume_micro
        elif relationship == "independent":
            if independent_reputation_eligible:
                self.independent_completed_jobs += 1
            else:
                self.unknown_completed_jobs += 1
            if independent_fee_volume_eligible:
                self.independent_fee_volume_micro += fee_volume_micro
            else:
                self.unknown_fee_volume_micro += fee_volume_micro
        else:
            self.unknown_completed_jobs += 1
            self.unknown_fee_volume_micro += fee_volume_micro


@dataclass
class PaperLedgerEntry:
    entry_id: str
    created_at: str
    debit_account: str
    credit_account: str
    amount_micro: int
    reason: str
    job_id: str | None = None
    payment_mode: str = "paper"
    settlement_status: str = "simulated"
    previous_hash: str = "sha256:" + ("0" * 64)
    record_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return _from_mapping(cls, raw)


@dataclass(frozen=True)
class WorkerCandidate:
    did: str
    evidence_ids: list[str] = field(default_factory=list)
    notes: str = ""
    operator_group: str | None = None
    source: str = "stub"


@dataclass(frozen=True)
class ExecutionPlan:
    worker: dict[str, str]
    settlement_plan: dict[str, Any]
    verification_plan: dict[str, Any]
    security_policy: dict[str, str]
    qualification: str
    reasons: list[str]
    selected_offer_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_route": dict(self.worker),
            "settlement_plan": dict(self.settlement_plan),
            "verification_plan": dict(self.verification_plan),
            "security_policy": dict(self.security_policy),
            "qualification": self.qualification,
            "reasons": list(self.reasons),
            "selected_offer_id": self.selected_offer_id,
        }


@dataclass(frozen=True)
class SentinelVerdict:
    action: Literal["ALLOW", "REJECT", "REVIEW"]
    signals: list[str]
    reasons: list[str]
    fail_closed: bool = True

    @property
    def allowed(self) -> bool:
        return self.action == "ALLOW"


@dataclass(frozen=True)
class BenchVerdict:
    result: Literal["PASS", "FAIL", "PARTIAL"]
    evidence_id: str | None
    notes: str
    local_exec: bool = False


@dataclass(frozen=True)
class TclkPaperDeal:
    deal_id: str
    mode: Literal["SIMULATION_ONLY"] = "SIMULATION_ONLY"
    settlement_execution: Literal["DISABLED"] = "DISABLED"
    payment_mode: Literal["paper"] = "paper"
    protocol: str = "tclk/1"


def did_slug(did: str) -> str:
    if not is_valid_ed25519_did(did):
        return "invalid-did"
    return did.replace(":", "_")
