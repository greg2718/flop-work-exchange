from __future__ import annotations

from flop_work_exchange.canonical import sha256_json
from flop_work_exchange.exceptions import SafetyError
from flop_work_exchange.models import Job, Offer, TclkPaperDeal
from flop_work_exchange.policy import reject_payment_proof_claims


class StubTclkAdapter:
    """Paper TCLK adapter. Simulation only; settlement execution DISABLED.

    Does not implement the normative TCLK state machine, sign TCLK frames,
    lock funds, or write to Technocore. Records a local paper deal id.
    """

    def record_paper_deal(self, job: Job, offer: Offer) -> TclkPaperDeal:
        reject_payment_proof_claims(job.outcome)
        reject_payment_proof_claims(offer.notes)
        if job.payment_mode != "paper":
            raise SafetyError("TCLK adapter only records paper deals")
        digest = sha256_json(
            {
                "job_id": job.job_id,
                "offer_id": offer.offer_id,
                "buyer_did": job.buyer_did,
                "seller_did": offer.seller_did,
                "price_micro": offer.price_micro,
                "mode": "SIMULATION_ONLY",
            }
        )
        return TclkPaperDeal(deal_id=f"tclk-paper-{digest[:20]}")
