from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from flop_work_exchange.canonical import atomic_write_json, load_json_object, sha256_json
from flop_work_exchange.constants import assert_isolated_state_dir
from flop_work_exchange.exceptions import StateError, ValidationError
from flop_work_exchange.models import (
    Deal,
    EvidenceProfile,
    Job,
    Offer,
    PaperLedgerEntry,
    Receipt,
    did_slug,
)


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


class ExchangeStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = assert_isolated_state_dir(state_dir)
        self.jobs_dir = self.state_dir / "jobs"
        self.offers_dir = self.state_dir / "offers"
        self.deals_dir = self.state_dir / "deals"
        self.receipts_dir = self.state_dir / "receipts"
        self.profiles_dir = self.state_dir / "evidence_profiles"
        self.ledger_path = self.state_dir / "paper_ledger.jsonl"

    def initialize(self) -> None:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in (
            self.jobs_dir,
            self.offers_dir,
            self.deals_dir,
            self.receipts_dir,
            self.profiles_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if not self.ledger_path.exists():
            self.ledger_path.write_text("", encoding="utf-8")
            self.ledger_path.chmod(0o600)

    def save_job(self, job: Job) -> None:
        atomic_write_json(self.jobs_dir / f"{job.job_id}.json", job.to_dict())

    def load_job(self, job_id: str) -> Job:
        path = self.jobs_dir / f"{job_id}.json"
        if not path.exists():
            raise StateError(f"unknown job: {job_id}")
        return Job.from_dict(load_json_object(path))

    def list_jobs(self) -> list[Job]:
        jobs = [
            Job.from_dict(load_json_object(path))
            for path in sorted(self.jobs_dir.glob("FLOP-JOB-*.json"))
        ]
        return jobs

    def save_offer(self, offer: Offer) -> None:
        atomic_write_json(self.offers_dir / f"{offer.offer_id}.json", offer.to_dict())

    def load_offer(self, offer_id: str) -> Offer:
        path = self.offers_dir / f"{offer_id}.json"
        if not path.exists():
            raise StateError(f"unknown offer: {offer_id}")
        return Offer.from_dict(load_json_object(path))

    def list_offers(self, job_id: str | None = None) -> list[Offer]:
        offers = [
            Offer.from_dict(load_json_object(path))
            for path in sorted(self.offers_dir.glob("FLOP-OFFER-*.json"))
        ]
        if job_id is not None:
            offers = [offer for offer in offers if offer.job_id == job_id]
        return offers

    def save_deal(self, deal: Deal) -> None:
        atomic_write_json(self.deals_dir / f"{deal.deal_id}.json", deal.to_dict())

    def load_deal(self, deal_id: str) -> Deal:
        path = self.deals_dir / f"{deal_id}.json"
        if not path.exists():
            raise StateError(f"unknown deal: {deal_id}")
        return Deal.from_dict(load_json_object(path))

    def list_deals(self) -> list[Deal]:
        return [
            Deal.from_dict(load_json_object(path))
            for path in sorted(self.deals_dir.glob("FLOP-DEAL-*.json"))
        ]

    def save_receipt(self, receipt: Receipt) -> Path:
        path = self.receipts_dir / f"{receipt.job_id}.json"
        atomic_write_json(path, receipt.to_dict())
        return path

    def load_receipt(self, job_id: str) -> dict[str, Any]:
        path = self.receipts_dir / f"{job_id}.json"
        if not path.exists():
            raise StateError(f"no receipt for job: {job_id}")
        return load_json_object(path)

    def load_profile(self, did: str) -> EvidenceProfile:
        path = self.profiles_dir / f"{did_slug(did)}.json"
        if not path.exists():
            return EvidenceProfile(did=did)
        return EvidenceProfile.from_dict(load_json_object(path))

    def save_profile(self, profile: EvidenceProfile) -> None:
        atomic_write_json(self.profiles_dir / f"{did_slug(profile.did)}.json", profile.to_dict())

    def append_ledger(self, entry: PaperLedgerEntry) -> PaperLedgerEntry:
        previous = self.latest_ledger_hash()
        entry.previous_hash = previous
        body = entry.to_dict()
        body["record_hash"] = ""
        entry.record_hash = "sha256:" + sha256_json(body)
        line = json.dumps(entry.to_dict(), sort_keys=True, separators=(",", ":"))
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return entry

    def ledger_entries(self) -> list[PaperLedgerEntry]:
        if not self.ledger_path.exists():
            return []
        entries: list[PaperLedgerEntry] = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entries.append(PaperLedgerEntry.from_dict(json.loads(line)))
        return entries

    def latest_ledger_hash(self) -> str:
        entries = self.ledger_entries()
        if not entries:
            return "sha256:" + ("0" * 64)
        return entries[-1].record_hash

    def paper_balance(self, account: str) -> int:
        balance = 0
        for entry in self.ledger_entries():
            if entry.credit_account == account:
                balance += entry.amount_micro
            if entry.debit_account == account:
                balance -= entry.amount_micro
        return balance

    def deal_counterparty_pairs(self) -> list[tuple[str, str]]:
        return [(deal.buyer_did, deal.seller_did) for deal in self.list_deals()]


def require_status(job: Job, allowed: set[str], action: str) -> None:
    if job.status not in allowed:
        raise ValidationError(
            f"cannot {action} job {job.job_id} in status {job.status}; "
            f"allowed: {', '.join(sorted(allowed))}"
        )
