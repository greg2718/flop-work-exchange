from __future__ import annotations

from pathlib import Path
from typing import Any

from flop_work_exchange.adapters import BenchAdapter, RouterAdapter, ScoutAdapter, SentinelAdapter
from flop_work_exchange.adapters.factory import resolve_adapters
from flop_work_exchange.adapters.settlement import PaperSettlement, TestnetSettlement
from flop_work_exchange.adapters.tclk import StubTclkAdapter
from flop_work_exchange.amounts import micro_to_flop_string, parse_flop_to_micro
from flop_work_exchange.canonical import result_hash_for
from flop_work_exchange.config import ExchangeConfig, load_config, write_resolved_config
from flop_work_exchange.exceptions import AdapterError, NotLiveError, SafetyError, ValidationError
from flop_work_exchange.identity import (
    ensure_test_identity,
    load_exchange_key,
    require_did,
)
from flop_work_exchange.models import (
    Deal,
    ExecutionPlan,
    Job,
    JobStatus,
    Offer,
    Receipt,
    WorkerCandidate,
    now_iso,
)
from flop_work_exchange.policy import (
    apply_deal_policy,
    classify_operator_relationship,
    detect_wash_risk,
    reject_payment_proof_claims,
)
from flop_work_exchange.receipts import sign_receipt, verify_receipt
from flop_work_exchange.store import ExchangeStore, new_id, require_status

ALLOWED = {
    "submit_offer": {JobStatus.POSTED.value, JobStatus.OFFERED.value},
    "accept_offer": {JobStatus.POSTED.value, JobStatus.OFFERED.value},
    "submit_result": {JobStatus.ACCEPTED.value},
    "verify": {JobStatus.RESULT_SUBMITTED.value},
    "settle": {JobStatus.VERIFIED.value},
    "refund": {
        JobStatus.ACCEPTED.value,
        JobStatus.RESULT_SUBMITTED.value,
        JobStatus.FAILED.value,
    },
}

EXCHANGE_FEE_ACCOUNT = "exchange-fees"
BENCH_FEE_ACCOUNT = "bench-validation-fees"


class WorkExchange:
    def __init__(
        self,
        config: ExchangeConfig,
        *,
        scout: ScoutAdapter | None = None,
        router: RouterAdapter | None = None,
        sentinel: SentinelAdapter | None = None,
        bench: BenchAdapter | None = None,
        tclk: StubTclkAdapter | None = None,
    ) -> None:
        self.config = config
        self.store = ExchangeStore(config.resolved_state_dir())
        self.store.initialize()
        write_resolved_config(self.store.state_dir, config)
        resolved = resolve_adapters(config.adapters)
        self.scout = scout if scout is not None else resolved.scout
        self.router = router if router is not None else resolved.router
        self.sentinel = sentinel if sentinel is not None else resolved.sentinel
        self.bench = bench if bench is not None else resolved.bench
        self.tclk = tclk or StubTclkAdapter()
        if config.settlement_backend == "testnet":
            self.settlement: PaperSettlement | TestnetSettlement = TestnetSettlement()
        else:
            self.settlement = PaperSettlement(self.store)

    @classmethod
    def open(cls, state_dir: Path, config_path: Path | None = None) -> WorkExchange:
        config = load_config(state_dir, config_path)
        return cls(config)

    def identity_meta(self) -> dict[str, Any]:
        return ensure_test_identity(self.store.state_dir)

    def exchange_did(self) -> str:
        return str(self.identity_meta()["did"])

    def credit_paper(self, account: str, amount_flop: str, reason: str = "paper-seed") -> None:
        self.settlement.credit(account, parse_flop_to_micro(amount_flop), reason)

    def post_job(
        self,
        *,
        buyer_did: str,
        outcome: str,
        service: str,
        budget_flop: str | None = None,
        budget_micro: int | None = None,
    ) -> Job:
        require_did(buyer_did)
        reject_payment_proof_claims(outcome)
        reject_payment_proof_claims(service)
        if budget_micro is None:
            if budget_flop is None:
                raise ValidationError("budget_flop or budget_micro is required")
            budget_micro = parse_flop_to_micro(budget_flop)
        if budget_micro <= 0:
            raise ValidationError("budget must be positive")
        verdict = self.sentinel.screen(
            "job",
            {"outcome": outcome, "service": service, "buyer_did": buyer_did},
        )
        if verdict.action == "REJECT":
            raise AdapterError(f"Sentinel REJECT on job: {', '.join(verdict.reasons)}")
        job = Job(
            job_id=new_id("FLOP-JOB"),
            buyer_did=buyer_did,
            outcome=outcome,
            service=service,
            budget_micro=budget_micro,
            payment_mode=self.config.payment_mode,
            sentinel_status=verdict.action,
            notes=list(verdict.reasons),
        )
        self.store.save_job(job)
        self.settlement.transfer(
            debit_account=buyer_did,
            credit_account=EXCHANGE_FEE_ACCOUNT,
            amount_micro=self.config.fees.job_posting_micro,
            reason="job-posting-fee",
            job_id=job.job_id,
        )
        return job

    def list_jobs(self) -> list[Job]:
        return self.store.list_jobs()

    def find_candidates(self, job_id: str) -> list[WorkerCandidate]:
        job = self.store.load_job(job_id)
        return self.scout.find_candidates(job)

    def submit_offer(
        self,
        *,
        job_id: str,
        seller_did: str,
        price_flop: str | None = None,
        price_micro: int | None = None,
        notes: str = "",
    ) -> Offer:
        require_did(seller_did)
        job = self.store.load_job(job_id)
        require_status(job, ALLOWED["submit_offer"], "submit_offer")
        reject_payment_proof_claims(notes)
        if price_micro is None:
            if price_flop is None:
                raise ValidationError("price_flop or price_micro is required")
            price_micro = parse_flop_to_micro(price_flop)
        if price_micro <= 0:
            raise ValidationError("offer price must be positive")
        if price_micro > job.budget_micro:
            raise ValidationError("offer exceeds job budget")
        verdict = self.sentinel.screen(
            "offer",
            {
                "job_id": job.job_id,
                "seller_did": seller_did,
                "price_micro": price_micro,
                "notes": notes,
            },
        )
        if verdict.action == "REJECT":
            raise AdapterError(f"Sentinel REJECT on offer: {', '.join(verdict.reasons)}")
        offer = Offer(
            offer_id=new_id("FLOP-OFFER"),
            job_id=job.job_id,
            seller_did=seller_did,
            price_micro=price_micro,
            sentinel_status=verdict.action,
            notes=notes,
        )
        self.store.save_offer(offer)
        job.status = JobStatus.OFFERED.value
        self.store.save_job(job)
        return offer

    def route_job(self, job_id: str) -> ExecutionPlan:
        job = self.store.load_job(job_id)
        offers = self.store.list_offers(job_id)
        return self.router.plan(job, offers)

    def accept_offer(self, offer_id: str, *, relationship: str | None = None) -> Deal:
        offer = self.store.load_offer(offer_id)
        job = self.store.load_job(offer.job_id)
        require_status(job, ALLOWED["accept_offer"], "accept_offer")
        if offer.status != "OPEN":
            raise ValidationError(f"offer {offer_id} is not OPEN")
        plan = self.router.plan(job, self.store.list_offers(job.job_id))
        if plan.qualification != "QUALIFIED_PLAN" or plan.selected_offer_id != offer.offer_id:
            raise AdapterError("Router did not select this offer: " + "; ".join(plan.reasons))
        classified = classify_operator_relationship(
            job.buyer_did, offer.seller_did, config=self.config
        )
        if relationship is not None:
            if relationship not in {"independent", "same_operator", "related", "unknown"}:
                raise ValidationError("invalid operator_relationship")
            if classified == "same_operator" and relationship == "independent":
                raise SafetyError(
                    "cannot present same-operator family DIDs as independent counterparties"
                )
            classified = relationship  # type: ignore[assignment]
        wash_risk = detect_wash_risk(
            buyer_did=job.buyer_did,
            seller_did=offer.seller_did,
            prior_pairs=self.store.deal_counterparty_pairs(),
        )
        flags = apply_deal_policy(
            buyer_did=job.buyer_did,
            seller_did=offer.seller_did,
            relationship=classified,
            policy=self.config.policy,
            wash_risk=wash_risk,
        )
        paper = self.tclk.record_paper_deal(job, offer)
        security = dict(plan.security_policy)
        security["sentinel_offer"] = offer.sentinel_status or "NOT_EVALUATED"
        deal = Deal(
            deal_id=new_id("FLOP-DEAL"),
            job_id=job.job_id,
            offer_id=offer.offer_id,
            buyer_did=job.buyer_did,
            seller_did=offer.seller_did,
            operator_relationship=classified,
            price_micro=offer.price_micro,
            payment_mode="paper",
            tclk_deal_id=paper.deal_id,
            tclk_mode=paper.mode,
            settlement_execution=paper.settlement_execution,
            independent_reputation_eligible=flags["independent_reputation_eligible"],
            independent_fee_volume_eligible=flags["independent_fee_volume_eligible"],
            wash_risk=flags["wash_risk"],
            work_route=dict(plan.worker),
            settlement_plan=dict(plan.settlement_plan),
            verification_plan=dict(plan.verification_plan),
            security_policy=security,
        )
        deal.settlement_plan["deal_id"] = paper.deal_id
        self.store.save_deal(deal)
        offer.status = "ACCEPTED"
        self.store.save_offer(offer)
        for other in self.store.list_offers(job.job_id):
            if other.offer_id != offer.offer_id and other.status == "OPEN":
                other.status = "DECLINED"
                self.store.save_offer(other)
        job.status = JobStatus.ACCEPTED.value
        job.accepted_offer_id = offer.offer_id
        job.deal_id = deal.deal_id
        self.store.save_job(job)
        return deal

    def submit_result(self, job_id: str, result_text: str, result_hash: str | None = None) -> Job:
        job = self.store.load_job(job_id)
        require_status(job, ALLOWED["submit_result"], "submit_result")
        reject_payment_proof_claims(result_text)
        digest = result_hash_for(result_text)
        if result_hash and result_hash != digest:
            raise ValidationError("provided result_hash does not match result_text")
        verdict = self.sentinel.screen(
            "result",
            {"job_id": job.job_id, "result_text": result_text, "result_hash": digest},
        )
        if verdict.action == "REJECT":
            raise AdapterError(f"Sentinel REJECT on result: {', '.join(verdict.reasons)}")
        job.result_text = result_text
        job.result_hash = digest
        job.status = JobStatus.RESULT_SUBMITTED.value
        job.sentinel_status = verdict.action
        self.store.save_job(job)
        return job

    def verify(self, job_id: str) -> Job:
        job = self.store.load_job(job_id)
        require_status(job, ALLOWED["verify"], "verify")
        verdict = self.bench.verify_delivery(job)
        job.bench_result = verdict.result
        if verdict.result == "PASS":
            job.status = JobStatus.VERIFIED.value
        else:
            job.status = JobStatus.FAILED.value
        job.notes.append(verdict.notes)
        self.store.save_job(job)
        return job

    def settle(self, job_id: str) -> tuple[Receipt, Path]:
        if self.config.settlement_backend != "paper":
            raise NotLiveError("settlement_execution is DISABLED for non-paper backends")
        job = self.store.load_job(job_id)
        require_status(job, ALLOWED["settle"], "settle")
        if not job.deal_id:
            raise ValidationError("job has no deal")
        deal = self.store.load_deal(job.deal_id)
        if job.bench_result != "PASS" or not job.result_hash:
            raise ValidationError("job is not Bench-PASS with a result_hash")
        fees = self.config.fees
        self.settlement.transfer(
            debit_account=job.buyer_did,
            credit_account=deal.seller_did,
            amount_micro=deal.price_micro,
            reason="worker-payout",
            job_id=job.job_id,
        )
        self.settlement.transfer(
            debit_account=job.buyer_did,
            credit_account=EXCHANGE_FEE_ACCOUNT,
            amount_micro=fees.orchestration_micro,
            reason="orchestration-fee",
            job_id=job.job_id,
        )
        self.settlement.transfer(
            debit_account=job.buyer_did,
            credit_account=EXCHANGE_FEE_ACCOUNT,
            amount_micro=fees.completion_micro,
            reason="completion-fee",
            job_id=job.job_id,
        )
        self.settlement.transfer(
            debit_account=job.buyer_did,
            credit_account=BENCH_FEE_ACCOUNT,
            amount_micro=fees.bench_validation_micro,
            reason="bench-validation-fee-stub",
            job_id=job.job_id,
        )
        completed_at = now_iso()
        receipt = Receipt(
            job_id=job.job_id,
            buyer_did=job.buyer_did,
            seller_did=deal.seller_did,
            operator_relationship=deal.operator_relationship,
            service=job.service,
            price_flop=micro_to_flop_string(deal.price_micro),
            payment_mode="paper",
            tclk_deal_id=deal.tclk_deal_id or "",
            result_hash=job.result_hash,
            bench_result=job.bench_result or "PASS",
            completed_at=completed_at,
            settlement_status="simulated",
            price_micro=deal.price_micro,
            independent_reputation_eligible=deal.independent_reputation_eligible,
            independent_fee_volume_eligible=deal.independent_fee_volume_eligible,
        )
        key, exchange_did = load_exchange_key(self.store.state_dir)
        sign_receipt(receipt, key, exchange_did)
        path = self.store.save_receipt(receipt)
        job.status = JobStatus.COMPLETED.value
        job.receipt_id = receipt.receipt_id
        self.store.save_job(job)
        for did in {job.buyer_did, deal.seller_did}:
            profile = self.store.load_profile(did)
            profile.record_completion(
                relationship=deal.operator_relationship,
                fee_volume_micro=deal.price_micro,
                receipt_id=receipt.receipt_id,
                job_id=job.job_id,
                result_hash=job.result_hash,
                bench_result=job.bench_result or "PASS",
                independent_reputation_eligible=deal.independent_reputation_eligible,
                independent_fee_volume_eligible=deal.independent_fee_volume_eligible,
                wash_risk=deal.wash_risk,
            )
            self.store.save_profile(profile)
        return receipt, path

    def refund(self, job_id: str, reason: str) -> Job:
        job = self.store.load_job(job_id)
        require_status(job, ALLOWED["refund"], "refund")
        if self.config.fees.refund_posting_fee:
            self.settlement.transfer(
                debit_account=EXCHANGE_FEE_ACCOUNT,
                credit_account=job.buyer_did,
                amount_micro=self.config.fees.job_posting_micro,
                reason=f"posting-fee-refund:{reason}",
                job_id=job.job_id,
            )
        job.status = JobStatus.REFUNDED.value
        job.notes.append(f"refund:{reason}")
        self.store.save_job(job)
        return job

    def show_receipt(self, job_id: str) -> dict[str, Any]:
        payload = self.store.load_receipt(job_id)
        verification = verify_receipt(payload)
        payload["verification"] = verification
        return payload

    def balances(self) -> dict[str, int]:
        accounts: dict[str, int] = {}
        for entry in self.store.ledger_entries():
            accounts.setdefault(entry.debit_account, 0)
            accounts.setdefault(entry.credit_account, 0)
        return {account: self.store.paper_balance(account) for account in sorted(accounts)}
