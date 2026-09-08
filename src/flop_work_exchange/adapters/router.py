from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from flop_work_exchange.adapters.process import CommandResult, run_argv
from flop_work_exchange.constants import ROUTER_DECISION_CLI
from flop_work_exchange.exceptions import AdapterError
from flop_work_exchange.models import ExecutionPlan, Job, Offer

CommandRunner = Callable[..., CommandResult]

# Expected local CLI (flop-router is a single-file script, not a package):
#   python router.py decision create "<task>" --output <file> \
#     --job-id <id> --job-proto flop-work-exchange.job.v0.1 \
#     --verification-mode OBJECTIVE_BENCH --asset FLOP --max-amount <micro>
#
# Artifact JSON keys consumed: work_route, settlement_plan, verification_plan,
# security_policy. TCLK planning remains SIMULATION_ONLY; settlement_execution
# is forced DISABLED. Router does not take Work Exchange offers as input;
# selected_offer_id is mapped from the Router worker DID onto open offers.


class StubRouterAdapter:
    """Offline Router adapter wrapping Router's decision shape.

    Real Router splits each decision into WORK_ROUTE / SETTLEMENT_PLAN /
    VERIFICATION_PLAN / SECURITY_POLICY. TCLK planning is SIMULATION_ONLY and
    settlement_execution is DISABLED. This stub does not import or execute
    flop-router; it returns the same document shape so wiring can be swapped
    later.
    """

    kind = "stub"

    def probe(self) -> dict[str, Any]:
        return {
            "ok": True,
            "kind": self.kind,
            "note": "offline lowest-offer route; not flop-router; settlement simulation only",
        }

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


class LocalRouterAdapter:
    """Best-effort wrapper around flop-router ``decision create``.

    flop-router is a single-file CLI (not an importable package). This adapter
    subprocesses the documented command and maps the decision artifact onto
    ``ExecutionPlan``. It never signs decisions, never posts to Technocore,
    and never executes settlement. Fail closed if the script or invocation is
    missing — never silently returns the stub plan labeled as live.
    """

    kind = "local"

    def __init__(
        self,
        *,
        script: Path | None = None,
        python: str = "python3",
        db_path: Path | None = None,
        cwd: Path | None = None,
        timeout_seconds: float = 60.0,
        run_command: CommandRunner | None = None,
        decision_fn: Callable[[Job, list[Offer]], dict[str, Any]] | None = None,
    ) -> None:
        self.script = script.expanduser() if script is not None else None
        self.python = python
        self.db_path = db_path.expanduser() if db_path is not None else None
        self.cwd = cwd.expanduser() if cwd is not None else None
        self.timeout_seconds = timeout_seconds
        self._run_command = run_command or run_argv
        self._decision_fn = decision_fn

    def probe(self) -> dict[str, Any]:
        if self._decision_fn is not None:
            return {
                "ok": True,
                "kind": self.kind,
                "cli_contract": ROUTER_DECISION_CLI,
                "note": "in-process decision function (tests)",
            }
        if self.script is None or not self.script.is_file():
            return {
                "ok": False,
                "kind": self.kind,
                "cli_contract": ROUTER_DECISION_CLI,
                "error": "router.py missing; set FLOP_WX_ROUTER_SCRIPT or FLOP_WX_ROUTER_REPO",
            }
        return {
            "ok": True,
            "kind": self.kind,
            "cli_contract": ROUTER_DECISION_CLI,
            "script": str(self.script),
            "db_path": str(self.db_path) if self.db_path else None,
            "note": "subprocess decision create; SIMULATION_ONLY / DISABLED enforced",
        }

    def plan(self, job: Job, offers: list[Offer]) -> ExecutionPlan:
        artifact = self._load_decision(job, offers)
        return map_router_decision(artifact, job, offers)

    def _load_decision(self, job: Job, offers: list[Offer]) -> dict[str, Any]:
        if self._decision_fn is not None:
            artifact = self._decision_fn(job, offers)
            if not isinstance(artifact, dict):
                raise AdapterError("Router decision function did not return a JSON object")
            return artifact
        if self.script is None or not self.script.is_file():
            raise AdapterError(
                "LocalRouterAdapter: flop-router script missing. Set FLOP_WX_ROUTER_SCRIPT "
                f"or FLOP_WX_ROUTER_REPO. Expected CLI: {ROUTER_DECISION_CLI}"
            )
        with tempfile.TemporaryDirectory(prefix="flop-wx-router-") as raw_tmp:
            output = Path(raw_tmp) / "decision.json"
            argv = self._decision_argv(job, output)
            result = self._run_command(
                argv,
                timeout_seconds=self.timeout_seconds,
                cwd=self.cwd or self.script.parent,
            )
            if result.returncode != 0:
                raise AdapterError(
                    "flop-router decision create failed "
                    f"(exit {result.returncode}): {_brief(result.stderr or result.stdout)}"
                )
            if not output.is_file():
                raise AdapterError(
                    "flop-router decision create did not write --output "
                    f"({_brief(result.stdout or result.stderr)})"
                )
            try:
                loaded = json.loads(output.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise AdapterError("flop-router decision artifact is not JSON") from exc
            if not isinstance(loaded, dict):
                raise AdapterError("flop-router decision artifact must be a JSON object")
            return loaded

    def _decision_argv(self, job: Job, output: Path) -> list[str]:
        assert self.script is not None
        argv = [
            self.python,
            str(self.script),
            "decision",
            "create",
            job.outcome,
            "--output",
            str(output),
            "--job-id",
            job.job_id,
            "--job-proto",
            "flop-work-exchange.job.v0.1",
            "--verification-mode",
            "OBJECTIVE_BENCH",
            "--asset",
            "FLOP",
            "--max-amount",
            str(job.budget_micro),
        ]
        if self.db_path is not None:
            argv.extend(["--db", str(self.db_path)])
        return argv


def map_router_decision(artifact: dict[str, Any], job: Job, offers: list[Offer]) -> ExecutionPlan:
    worker = _string_map(artifact.get("work_route") or artifact.get("worker") or {})
    settlement = dict(artifact.get("settlement_plan") or {})
    verification = dict(artifact.get("verification_plan") or {})
    security = _string_map(artifact.get("security_policy") or {})
    settlement["protocol"] = str(settlement.get("protocol") or "tclk/1")
    settlement["mode"] = "SIMULATION_ONLY"
    settlement["settlement_execution"] = "DISABLED"
    verification.setdefault("mode", "OBJECTIVE_BENCH")
    verification.setdefault("required", True)
    verification["job_id"] = job.job_id
    verification.setdefault("job_proto", "flop-work-exchange.job.v0.1")
    qualification = str(artifact.get("qualification") or _qualification_from_worker(worker))
    reasons = _reason_list(artifact.get("reasons"))
    if not reasons:
        reasons = ["flop-router decision create; settlement simulation only"]
    worker_did = worker.get("did") or "none"
    selected = _matching_open_offer(worker_did, offers)
    if qualification == "QUALIFIED_PLAN" and selected is None:
        return ExecutionPlan(
            worker=worker or {"did": worker_did, "capability_support": "no matching offer"},
            settlement_plan=settlement,
            verification_plan=verification,
            security_policy=security or {"status": "NOT_EVALUATED"},
            qualification="DISQUALIFIED",
            reasons=[
                *reasons,
                "router-selected worker has no open Work Exchange offer; "
                "not substituting a stub offer as a live route",
            ],
        )
    if selected is not None:
        worker.setdefault("did", selected.seller_did)
    return ExecutionPlan(
        worker=worker or {"did": worker_did, "capability_support": "router-work-route"},
        settlement_plan=settlement,
        verification_plan=verification,
        security_policy=security or {"status": "NOT_EVALUATED"},
        qualification=qualification,
        reasons=reasons,
        selected_offer_id=selected.offer_id if selected is not None else None,
    )


def _qualification_from_worker(worker: dict[str, str]) -> str:
    did = worker.get("did")
    if not did or did == "none":
        return "DISQUALIFIED"
    return "QUALIFIED_PLAN"


def _matching_open_offer(worker_did: str, offers: Sequence[Offer]) -> Offer | None:
    if not worker_did or worker_did == "none":
        return None
    matches = [
        offer
        for offer in offers
        if offer.status == "OPEN" and offer.seller_did == worker_did
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: (item.price_micro, item.offer_id))[0]


def _string_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _reason_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str) and raw:
        return [raw]
    return []


def _brief(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "…"
