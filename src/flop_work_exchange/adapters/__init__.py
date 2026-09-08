"""Adapter interfaces for Scout, Bench, Router, Sentinel, TCLK, and settlement.

These are adapters only. They do not reimplement the sibling agents. Stubs
work fully offline and return the sibling decision/verdict shapes so a later
wiring pass can call the real packages without changing exchange orchestration.
"""

from __future__ import annotations

from typing import Protocol

from flop_work_exchange.models import (
    BenchVerdict,
    ExecutionPlan,
    Job,
    Offer,
    SentinelVerdict,
    TclkPaperDeal,
    WorkerCandidate,
)


class ScoutAdapter(Protocol):
    def find_candidates(self, job: Job) -> list[WorkerCandidate]: ...


class RouterAdapter(Protocol):
    def plan(self, job: Job, offers: list[Offer]) -> ExecutionPlan: ...


class SentinelAdapter(Protocol):
    def screen(self, artifact_type: str, artifact: dict[str, object]) -> SentinelVerdict: ...


class BenchAdapter(Protocol):
    def verify_delivery(self, job: Job) -> BenchVerdict: ...


class TclkAdapter(Protocol):
    def record_paper_deal(self, job: Job, offer: Offer) -> TclkPaperDeal: ...


class SettlementAdapter(Protocol):
    backend: str

    def credit(
        self, account: str, amount_micro: int, reason: str, job_id: str | None = None
    ) -> None: ...

    def transfer(
        self,
        *,
        debit_account: str,
        credit_account: str,
        amount_micro: int,
        reason: str,
        job_id: str | None = None,
    ) -> None: ...
