from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from flop_work_exchange import __version__
from flop_work_exchange.adapters.scout import candidates_payload
from flop_work_exchange.config import AdapterConfig, load_adapter_config, load_config
from flop_work_exchange.constants import DEFAULT_PRODUCTION_STATE, MAX_CLI_JSON_CHARS
from flop_work_exchange.demo import run_demo
from flop_work_exchange.exceptions import WorkExchangeError
from flop_work_exchange.exchange import WorkExchange
from flop_work_exchange.identity import (
    create_production_identity,
    create_test_identity,
    load_identity_meta,
)
from flop_work_exchange.ops import doctor, run_live_demo
from flop_work_exchange.receipts import verify_receipt


def _print_json(value: Any, *, max_chars: int | None = None) -> None:
    text = json.dumps(value, indent=2, sort_keys=True, default=str)
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n... [truncated]"
    print(text)


def _adapter_config_from_args(args: argparse.Namespace) -> AdapterConfig:
    config_path = Path(args.config) if getattr(args, "config", None) else None
    return load_adapter_config(config_path)


def _require_state_dir(args: argparse.Namespace) -> Path:
    if not getattr(args, "state_dir", None):
        raise SystemExit("--state-dir is required (demos/tests should pass a temp dir)")
    return Path(args.state_dir)


def _open(args: argparse.Namespace) -> WorkExchange:
    state_dir = _require_state_dir(args)
    config_path = Path(args.config) if getattr(args, "config", None) else None
    return WorkExchange(load_config(state_dir, config_path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flop-work-exchange",
        description="Paper-FLOP job marketplace. Settlement execution is DISABLED.",
    )
    parser.add_argument("--version", action="version", version=f"flop-work-exchange {__version__}")
    parser.add_argument(
        "--state-dir",
        type=Path,
        help=(
            "Local state directory. Required for write commands. Production path is "
            f"{DEFAULT_PRODUCTION_STATE}; demos and tests must pass a temp dir."
        ),
    )
    parser.add_argument("--config", type=Path, help="Fee/policy config (JSON, YAML, or TOML)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    identity = sub.add_parser("identity", help="Exchange Ed25519 / did:key identity")
    identity_sub = identity.add_subparsers(dest="identity_cmd", required=True)
    identity_sub.add_parser("init", help="Create a test-only identity in --state-dir")
    prod = identity_sub.add_parser("init-production", help="Gated production identity (encrypted)")
    prod.add_argument("--confirm", required=True)
    identity_sub.add_parser("show", help="Show public identity metadata")

    post = sub.add_parser("post-job", help="Buyer posts an outcome + budget")
    post.add_argument("--buyer-did", required=True)
    post.add_argument("--outcome", required=True)
    post.add_argument("--service", required=True)
    post.add_argument("--budget-flop", required=True, help="Exact integer or decimal FLOP string")

    sub.add_parser("list-jobs", help="List local jobs")

    offer = sub.add_parser("submit-offer", help="Worker submits an offer")
    offer.add_argument("--job-id", required=True)
    offer.add_argument("--seller-did", required=True)
    offer.add_argument("--price-flop", required=True)
    offer.add_argument("--notes", default="")

    accept = sub.add_parser("accept-offer", help="Accept an offer and record a paper TCLK deal")
    accept.add_argument("--offer-id", required=True)
    accept.add_argument(
        "--operator-relationship",
        choices=["independent", "same_operator", "related", "unknown"],
        default=None,
    )

    result = sub.add_parser("submit-result", help="Worker submits result text")
    result.add_argument("--job-id", required=True)
    result.add_argument("--result", required=True)
    result.add_argument("--result-hash", default=None)

    verify = sub.add_parser("verify", help="Bench-adapter verify of submitted result")
    verify.add_argument("--job-id", required=True)

    settle = sub.add_parser("settle", help="Paper-settle a Bench-PASS job and sign a receipt")
    settle.add_argument("--job-id", required=True)

    show = sub.add_parser("show-receipt", help="Load and verify a signed receipt")
    show.add_argument("--job-id", required=True)

    verify_file = sub.add_parser("verify-receipt", help="Verify a receipt JSON file")
    verify_file.add_argument("receipt", type=Path)

    credit = sub.add_parser("paper-credit", help="Seed a paper ledger account (simulation only)")
    credit.add_argument("--account", required=True)
    credit.add_argument("--amount-flop", required=True)
    credit.add_argument("--reason", default="paper-seed")

    sub.add_parser("balances", help="Show paper ledger balances")

    candidates = sub.add_parser("find-candidates", help="Scout-adapter candidate lookup")
    candidates.add_argument("--job-id", required=True)
    candidates.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max candidates to print (default: adapters.scout_candidate_limit, 25)",
    )

    route = sub.add_parser("route", help="Router-adapter WORK_ROUTE / plans")
    route.add_argument("--job-id", required=True)

    demo = sub.add_parser("demo", help="Run one full paper job end-to-end")
    demo.add_argument(
        "--state-dir",
        dest="demo_state_dir",
        type=Path,
        default=None,
        help="Optional temp/state dir; created if omitted",
    )
    live_demo = sub.add_parser(
        "live-demo",
        help="Paper job preferring local Scout/Bench/Router/Sentinel adapters",
    )
    live_demo.add_argument(
        "--state-dir",
        dest="demo_state_dir",
        type=Path,
        default=None,
        help="Optional temp/state dir; created if omitted",
    )
    doc = sub.add_parser(
        "doctor",
        help="Check adapter modes, paths, identity, and state isolation",
    )
    doc.add_argument(
        "--state-dir",
        dest="doctor_state_dir",
        type=Path,
        default=None,
        help="Optional state dir to inspect (not created)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return _dispatch(args)
    except WorkExchangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.cmd == "demo":
        state_dir = args.demo_state_dir or args.state_dir
        if state_dir is None:
            state_dir = Path(tempfile.mkdtemp(prefix="flop-work-exchange-demo-"))
        result = run_demo(Path(state_dir))
        _print_json(result)
        return 0 if result.get("verification", {}).get("ok") else 2

    if args.cmd == "live-demo":
        state_dir = args.demo_state_dir or args.state_dir
        if state_dir is None:
            state_dir = Path(tempfile.mkdtemp(prefix="flop-work-exchange-live-demo-"))
        result = run_live_demo(
            Path(state_dir),
            adapter_config=_adapter_config_from_args(args),
            config_path=Path(args.config) if getattr(args, "config", None) else None,
        )
        _print_json(result, max_chars=MAX_CLI_JSON_CHARS)
        ok = bool(result.get("ok")) and bool(result.get("verification", {}).get("ok"))
        return 0 if ok else 2

    if args.cmd == "doctor":
        state_dir = args.doctor_state_dir or args.state_dir
        config_path = Path(args.config) if getattr(args, "config", None) else None
        report = doctor(
            state_dir=Path(state_dir) if state_dir is not None else None,
            adapter_config=_adapter_config_from_args(args),
            config_path=config_path,
        )
        _print_json(report)
        return 0 if report.get("ok") else 1

    if args.cmd == "identity":
        state_dir = _require_state_dir(args)
        if args.identity_cmd == "init":
            _print_json(create_test_identity(state_dir))
            return 0
        if args.identity_cmd == "init-production":
            import getpass

            first = getpass.getpass("New FLOP Work Exchange identity passphrase: ")
            second = getpass.getpass("Confirm passphrase: ")
            _print_json(
                create_production_identity(
                    state_dir=state_dir,
                    confirm=args.confirm,
                    passphrase=first,
                    passphrase_confirmation=second,
                )
            )
            return 0
        if args.identity_cmd == "show":
            _print_json(load_identity_meta(state_dir))
            return 0

    if args.cmd == "verify-receipt":
        payload = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
        _print_json(verify_receipt(payload))
        return 0

    exchange = _open(args)
    if args.cmd == "post-job":
        job = exchange.post_job(
            buyer_did=args.buyer_did,
            outcome=args.outcome,
            service=args.service,
            budget_flop=args.budget_flop,
        )
        _print_json(job.to_dict())
        return 0
    if args.cmd == "list-jobs":
        _print_json([job.to_dict() for job in exchange.list_jobs()])
        return 0
    if args.cmd == "submit-offer":
        offer = exchange.submit_offer(
            job_id=args.job_id,
            seller_did=args.seller_did,
            price_flop=args.price_flop,
            notes=args.notes,
        )
        _print_json(offer.to_dict())
        return 0
    if args.cmd == "accept-offer":
        deal = exchange.accept_offer(
            args.offer_id,
            relationship=args.operator_relationship,
        )
        _print_json(deal.to_dict())
        return 0
    if args.cmd == "submit-result":
        job = exchange.submit_result(args.job_id, args.result, args.result_hash)
        _print_json(job.to_dict())
        return 0
    if args.cmd == "verify":
        _print_json(exchange.verify(args.job_id).to_dict())
        return 0
    if args.cmd == "settle":
        receipt, path = exchange.settle(args.job_id)
        payload = receipt.to_dict()
        payload["receipt_path"] = str(path)
        _print_json(payload)
        return 0
    if args.cmd == "show-receipt":
        _print_json(exchange.show_receipt(args.job_id))
        return 0
    if args.cmd == "paper-credit":
        exchange.credit_paper(args.account, args.amount_flop, args.reason)
        _print_json({"ok": True, "account": args.account, "balances": exchange.balances()})
        return 0
    if args.cmd == "balances":
        _print_json(exchange.balances())
        return 0
    if args.cmd == "find-candidates":
        found = exchange.find_candidates(args.job_id, limit=args.limit)
        limit = args.limit or exchange.config.adapters.scout_candidate_limit
        _print_json(candidates_payload(found, limit=limit), max_chars=MAX_CLI_JSON_CHARS)
        return 0
    if args.cmd == "route":
        _print_json(exchange.route_job(args.job_id).to_dict())
        return 0
    raise SystemExit(f"unknown command: {args.cmd}")
