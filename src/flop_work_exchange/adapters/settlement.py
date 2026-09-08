from __future__ import annotations

from flop_work_exchange.exceptions import InsufficientFundsError, NotLiveError, SafetyError
from flop_work_exchange.models import PaperLedgerEntry, now_iso
from flop_work_exchange.store import ExchangeStore, new_id


class PaperSettlement:
    """Debit/credit local paper ledgers. Never claims real token movement."""

    backend = "paper"

    def __init__(self, store: ExchangeStore) -> None:
        self.store = store

    def credit(
        self, account: str, amount_micro: int, reason: str, job_id: str | None = None
    ) -> None:
        if amount_micro < 0:
            raise SafetyError("credit amount must be non-negative")
        if amount_micro == 0:
            return
        self.store.append_ledger(
            PaperLedgerEntry(
                entry_id=new_id("FLOP-LEDGER"),
                created_at=now_iso(),
                debit_account="paper-issuance",
                credit_account=account,
                amount_micro=amount_micro,
                reason=reason,
                job_id=job_id,
            )
        )

    def transfer(
        self,
        *,
        debit_account: str,
        credit_account: str,
        amount_micro: int,
        reason: str,
        job_id: str | None = None,
    ) -> None:
        if amount_micro < 0:
            raise SafetyError("transfer amount must be non-negative")
        if amount_micro == 0:
            return
        if debit_account == "paper-issuance":
            raise SafetyError("use credit() for paper issuance; transfers cannot mint")
        balance = self.store.paper_balance(debit_account)
        if balance < amount_micro:
            raise InsufficientFundsError(
                f"paper account {debit_account} has {balance} micro; need {amount_micro}"
            )
        self.store.append_ledger(
            PaperLedgerEntry(
                entry_id=new_id("FLOP-LEDGER"),
                created_at=now_iso(),
                debit_account=debit_account,
                credit_account=credit_account,
                amount_micro=amount_micro,
                reason=reason,
                job_id=job_id,
            )
        )


class TestnetSettlement:
    """Placeholder for a future live/testnet rail. Always raises NotLiveError."""

    backend = "testnet"

    def credit(
        self, account: str, amount_micro: int, reason: str, job_id: str | None = None
    ) -> None:
        raise NotLiveError(
            "TestnetSettlement is not live. Technocore has no faucet/balance/wallet/"
            "transfer/payment endpoints. payment_mode remains paper; settlement_execution=DISABLED."
        )

    def transfer(
        self,
        *,
        debit_account: str,
        credit_account: str,
        amount_micro: int,
        reason: str,
        job_id: str | None = None,
    ) -> None:
        raise NotLiveError(
            "Live FLOP transfers are not available. Do not treat faucet claims as payment. "
            "Use PaperSettlement (payment_mode=paper, settlement_status=simulated)."
        )
