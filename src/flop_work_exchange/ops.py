"""Ops helpers: doctor checks and the paper live-demo path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flop_work_exchange.adapters.bench import StubBenchAdapter
from flop_work_exchange.adapters.factory import resolve_adapters
from flop_work_exchange.adapters.router import StubRouterAdapter
from flop_work_exchange.adapters.scout import StubScoutAdapter
from flop_work_exchange.adapters.sentinel import StubSentinelAdapter
from flop_work_exchange.config import AdapterConfig, ExchangeConfig, PolicyConfig
from flop_work_exchange.constants import (
    BENCH_STATE,
    DEFAULT_PRODUCTION_STATE,
    DEFAULT_SCOUT_CANDIDATE_LIMIT,
    LEGACY_SCOUT_STATE,
    MAC_BENCH_REPO,
    MAC_ROUTER_REPO,
    MAC_SCOUT_REPO,
    MAC_SENTINEL_REPO,
    ROUTER_STATE,
    SCOUT_STATE,
    SENTINEL_STATE,
    sibling_state_dirs,
)
from flop_work_exchange.exceptions import AdapterError, IsolationError, ValidationError
from flop_work_exchange.exchange import WorkExchange
from flop_work_exchange.identity import (
    TEST_IDENTITY_JSON,
    create_ephemeral_party,
    ensure_test_identity,
    load_identity_meta,
)
from flop_work_exchange.receipts import verify_receipt


def doctor(
    *,
    state_dir: Path | None = None,
    adapter_config: AdapterConfig | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    adapters = adapter_config or AdapterConfig()
    bundle = resolve_adapters(adapters)
    checks: list[dict[str, Any]] = [
        {
            "name": "payment_mode",
            "ok": True,
            "value": "paper",
            "note": "live FLOP rails are not available; faucet/wallet/transfer are out of scope",
        },
        {
            "name": "tclk_mode",
            "ok": True,
            "value": "SIMULATION_ONLY",
        },
        {
            "name": "settlement_execution",
            "ok": True,
            "value": "DISABLED",
        },
        {
            "name": "bench_allow_local_exec",
            "ok": True,
            "value": adapters.bench_allow_local_exec,
            "note": "default false; never pass --allow-local-exec unless explicitly enabled",
        },
        _adapter_check("scout", adapters.scout_mode, bundle.scout.probe()),
        _adapter_check("bench", adapters.bench_mode, bundle.bench.probe()),
        _adapter_check("router", adapters.router_mode, bundle.router.probe()),
        _adapter_check("sentinel", adapters.sentinel_mode, bundle.sentinel.probe()),
        {
            "name": "mac_dev_paths",
            "ok": True,
            "value": {
                "scout": str(MAC_SCOUT_REPO),
                "bench": str(MAC_BENCH_REPO),
                "router": str(MAC_ROUTER_REPO),
                "sentinel": str(MAC_SENTINEL_REPO),
            },
            "exists": {
                "scout": MAC_SCOUT_REPO.exists(),
                "bench": MAC_BENCH_REPO.exists(),
                "router": MAC_ROUTER_REPO.exists(),
                "sentinel": MAC_SENTINEL_REPO.exists(),
            },
        },
        {
            "name": "sibling_state_isolation_rules",
            "ok": True,
            "forbidden": [str(path) for path in sibling_state_dirs()],
            "production_state": str(DEFAULT_PRODUCTION_STATE),
            "note": (
                "Work Exchange must not use Scout/Bench/Router/Sentinel state dirs. "
                "Live Bench verify uses a temp --state-dir."
            ),
        },
    ]
    if state_dir is not None:
        checks.append(_isolation_check(state_dir))
        checks.append(_identity_check(state_dir))
        checks.append(_overlap_live_backends(state_dir))
    ok = all(bool(check.get("ok", True)) for check in checks)
    return {
        "ok": ok,
        "payment_mode": "paper",
        "tclk_mode": "SIMULATION_ONLY",
        "settlement_execution": "DISABLED",
        "not_live": [
            "faucet",
            "wallet",
            "token transfer",
            "Technocore payment endpoints",
            "settlement_execution",
        ],
        "adapter_modes": {
            "scout": adapters.scout_mode,
            "bench": adapters.bench_mode,
            "router": adapters.router_mode,
            "sentinel": adapters.sentinel_mode,
        },
        "config_path": str(config_path) if config_path is not None else None,
        "scout_candidate_limit": adapters.scout_candidate_limit,
        "router_db": str(adapters.router_db) if adapters.router_db else None,
        "router_fixture": str(adapters.router_fixture) if adapters.router_fixture else None,
        "checks": checks,
    }


def run_live_demo(
    state_dir: Path,
    *,
    adapter_config: AdapterConfig | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Run one paper job, preferring local adapters that actually probe OK."""
    requested = adapter_config or AdapterConfig()
    notes: list[str] = []
    mid_run_errors: list[str] = []
    bundle = resolve_adapters(requested)
    scout, notes = _prefer_local(
        "scout", requested.scout_mode, bundle.scout, StubScoutAdapter(), notes
    )
    bench, notes = _prefer_local(
        "bench", requested.bench_mode, bundle.bench, StubBenchAdapter(), notes
    )
    sentinel, notes = _prefer_local(
        "sentinel", requested.sentinel_mode, bundle.sentinel, StubSentinelAdapter(), notes
    )
    router, notes = _prefer_local(
        "router", requested.router_mode, bundle.router, StubRouterAdapter(), notes
    )
    config = ExchangeConfig(
        state_dir=state_dir,
        payment_mode="paper",
        settlement_backend="paper",
        policy=PolicyConfig(treat_unknown_as_independent=False, allow_same_operator_deals=True),
        adapters=requested,
    )
    exchange = WorkExchange(
        config,
        scout=scout,
        router=router,
        sentinel=sentinel,
        bench=bench,
    )
    ensure_test_identity(state_dir)
    _buyer_key, buyer_did = create_ephemeral_party("buyer")
    _seller_key, seller_did = create_ephemeral_party("seller")
    exchange.credit_paper(buyer_did, "20", "demo-buyer-seed")
    job = exchange.post_job(
        buyer_did=buyer_did,
        outcome="Produce a one-line hash-locked summary of the paper settlement rules.",
        service="documentation.summary",
        budget_flop="12",
    )
    try:
        candidates = exchange.find_candidates(job.job_id)
    except AdapterError as exc:
        if getattr(exchange.scout, "kind", "") == "local":
            mid_run_errors.append(f"scout: {exc}")
        notes.append(f"scout: local find_candidates failed ({exc}); falling back to stub")
        exchange.scout = StubScoutAdapter()
        candidates = exchange.find_candidates(job.job_id)
    offer = exchange.submit_offer(
        job_id=job.job_id,
        seller_did=seller_did,
        price_flop="12",
        notes="paper live-demo worker offer",
    )
    try:
        plan = exchange.route_job(job.job_id)
    except AdapterError as exc:
        if getattr(exchange.router, "kind", "") == "local":
            mid_run_errors.append(f"router: {exc}")
        notes.append(f"router: local plan failed ({exc}); falling back to stub router")
        exchange.router = StubRouterAdapter()
        plan = exchange.route_job(job.job_id)
    if plan.qualification != "QUALIFIED_PLAN" or plan.selected_offer_id != offer.offer_id:
        notes.append(
            "router: local/live plan did not select the demo offer "
            f"(qualification={plan.qualification}); falling back to stub router"
        )
        exchange.router = StubRouterAdapter()
        plan = exchange.route_job(job.job_id)
    deal = exchange.accept_offer(offer.offer_id, relationship="independent")
    result_text = (
        "Paper settlement debits local ledgers only; payment_mode=paper; "
        "settlement_status=simulated; TCLK mode=SIMULATION_ONLY."
    )
    exchange.submit_result(job.job_id, result_text)
    try:
        exchange.verify(job.job_id)
    except AdapterError as exc:
        if getattr(exchange.bench, "kind", "") == "local":
            mid_run_errors.append(f"bench: {exc}")
        notes.append(f"bench: local verify failed ({exc}); falling back to stub")
        exchange.bench = StubBenchAdapter()
        job_row = exchange.store.load_job(job.job_id)
        job_row.status = "RESULT_SUBMITTED"
        exchange.store.save_job(job_row)
        exchange.verify(job.job_id)
    receipt, receipt_path = exchange.settle(job.job_id)
    payload = receipt.to_dict()
    verification = verify_receipt(payload)
    seller_profile = exchange.store.load_profile(seller_did).public_claims()
    limit = requested.scout_candidate_limit or DEFAULT_SCOUT_CANDIDATE_LIMIT
    candidate_dids = [candidate.did for candidate in candidates[:limit]]
    ok = bool(verification.get("ok")) and not mid_run_errors
    return {
        "ok": ok,
        "state_dir": str(state_dir),
        "config_path": str(config_path) if config_path is not None else None,
        "exchange_did": exchange.exchange_did(),
        "buyer_did": buyer_did,
        "seller_did": seller_did,
        "job_id": job.job_id,
        "offer_id": offer.offer_id,
        "deal_id": deal.deal_id,
        "tclk_deal_id": deal.tclk_deal_id,
        "operator_relationship": deal.operator_relationship,
        "independent_reputation_eligible": deal.independent_reputation_eligible,
        "candidates_from_scout": candidate_dids,
        "candidates_shown": len(candidate_dids),
        "candidates_limit": limit,
        "router_qualification": plan.qualification,
        "receipt_path": str(receipt_path),
        "receipt": payload,
        "verification": verification,
        "seller_public_claims": seller_profile,
        "balances": exchange.balances(),
        "payment_mode": "paper",
        "settlement_status": "simulated",
        "settlement_execution": "DISABLED",
        "adapter_kinds": {
            "scout": getattr(exchange.scout, "kind", "unknown"),
            "bench": getattr(exchange.bench, "kind", "unknown"),
            "router": getattr(exchange.router, "kind", "unknown"),
            "sentinel": getattr(exchange.sentinel, "kind", "unknown"),
        },
        "adapter_notes": notes,
        "adapter_errors": mid_run_errors,
        "not_live": ["faucet", "wallet", "token transfer", "settlement_execution"],
    }


def _prefer_local(
    name: str,
    mode: str,
    local_or_stub: Any,
    stub: Any,
    notes: list[str],
) -> tuple[Any, list[str]]:
    if mode != "local":
        notes.append(f"{name}: stub (default offline)")
        return stub, notes
    probe = local_or_stub.probe()
    if probe.get("ok"):
        notes.append(f"{name}: local ({probe.get('note') or 'probe ok'})")
        return local_or_stub, notes
    notes.append(
        f"{name}: local requested but unavailable ({probe.get('error')}); falling back to stub"
    )
    return stub, notes


def _adapter_check(name: str, mode: str, probe: dict[str, Any]) -> dict[str, Any]:
    ok = True if mode == "stub" else bool(probe.get("ok"))
    return {
        "name": f"adapter_{name}",
        "ok": ok,
        "mode": mode,
        "kind": probe.get("kind"),
        "probe": probe,
        "note": (
            "local mode fails closed when the sibling backend is missing; "
            "stub success is never labeled live"
            if mode == "local" and not probe.get("ok")
            else probe.get("note") or probe.get("error")
        ),
    }


def _isolation_check(state_dir: Path) -> dict[str, Any]:
    from flop_work_exchange.constants import assert_isolated_state_dir

    try:
        resolved = assert_isolated_state_dir(state_dir)
    except IsolationError as exc:
        return {"name": "state_isolation", "ok": False, "error": str(exc)}
    return {"name": "state_isolation", "ok": True, "state_dir": str(resolved)}


def _identity_check(state_dir: Path) -> dict[str, Any]:
    try:
        meta = load_identity_meta(state_dir)
    except (ValidationError, IsolationError) as exc:
        exists = (state_dir.expanduser() / TEST_IDENTITY_JSON).exists()
        return {
            "name": "identity",
            "ok": True,
            "present": exists,
            "note": str(exc) if not exists else str(exc),
        }
    return {
        "name": "identity",
        "ok": True,
        "present": True,
        "did": meta.get("did"),
        "purpose": meta.get("purpose"),
        "persistent": meta.get("persistent"),
        "note": "public metadata only; private key is not read",
    }


def _overlap_live_backends(state_dir: Path) -> dict[str, Any]:
    forbidden = {
        "scout": SCOUT_STATE,
        "legacy_scout": LEGACY_SCOUT_STATE,
        "bench": BENCH_STATE,
        "router": ROUTER_STATE,
        "sentinel": SENTINEL_STATE,
    }
    resolved = state_dir.expanduser().resolve(strict=False)
    overlaps = {
        name: str(path)
        for name, path in forbidden.items()
        if resolved == path.expanduser().resolve(strict=False)
    }
    return {
        "name": "no_sibling_state_overlap",
        "ok": not overlaps,
        "overlaps": overlaps,
    }
