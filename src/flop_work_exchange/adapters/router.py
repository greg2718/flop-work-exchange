from __future__ import annotations

from flop_work_exchange.models import ExecutionPlan, Job, Offer


class StubRouterAdapter:
    """Offline Router adapter wrapping Router's decision shape.

    Real Router splits each decision into WORK_ROUTE / SETTLEMENT_PLAN /
    VERIFICATION_PLAN / SECURITY_POLICY. TCLK planning is SIMULATION_ONLY and
    settlement_execution is DISABLED. This stub does not import or execute
    flop-router; it returns the same document shape so wiring can be swapped
    later.
    """

    def plan(self, job: Job, offers: list[Offer]) -> ExecutionPlan:
        open_offers = [offer for offer in offers if offer.status == "OPEN"]
        if not open_offers:
            return ExecutionPlan(
                worker={"did": "none", "capability_support": "no open offers"},
                settlement_plan={
                    "protocol": "tclk/1",
                    "status": "not planned",
                    "mode": "SIMULATION_ONLY",
                    "settlement_execution": "DISABLED",
                },
                verification_plan={
                    "mode": "OBJECTIVE_BENCH",
                    "required": True,
                    "job_id": job.job_id,
                },
                security_policy={"status": "NOT_EVALUATED"},
                qualification="DISQUALIFIED",
                reasons=["no open offers"],
            )
        chosen = sorted(open_offers, key=lambda item: (item.price_micro, item.offer_id))[0]
        if chosen.price_micro > job.budget_micro:
            return ExecutionPlan(
                worker={"did": chosen.seller_did, "capability_support": "over budget"},
                settlement_plan={
                    "protocol": "tclk/1",
                    "status": "NO_COMPATIBLE_SETTLEMENT_ROUTE",
                    "mode": "SIMULATION_ONLY",
                    "settlement_execution": "DISABLED",
                },
                verification_plan={
                    "mode": "OBJECTIVE_BENCH",
                    "required": True,
                    "job_id": job.job_id,
                },
                security_policy={"status": "NOT_EVALUATED"},
                qualification="DISQUALIFIED",
                reasons=["lowest offer exceeds job budget"],
                selected_offer_id=chosen.offer_id,
            )
        return ExecutionPlan(
            worker={
                "did": chosen.seller_did,
                "capability_support": "stub-offer-route",
            },
            settlement_plan={
                "protocol": "tclk/1",
                "mode": "SIMULATION_ONLY",
                "settlement_execution": "DISABLED",
                "rail": "paper",
                "lock": "none",
                "asset": "FLOP",
                "amount": str(chosen.price_micro),
                "confidence": "NO_EVIDENCE",
                "deal_id": "pending-paper",
                "status": "SIMULATED_PAPER",
            },
            verification_plan={
                "mode": "OBJECTIVE_BENCH",
                "required": True,
                "job_proto": "flop-work-exchange.job.v0.1",
                "job_id": job.job_id,
            },
            security_policy={"status": "PENDING_SENTINEL"},
            qualification="QUALIFIED_PLAN",
            reasons=["lowest in-budget stub offer selected; settlement simulation only"],
            selected_offer_id=chosen.offer_id,
        )
