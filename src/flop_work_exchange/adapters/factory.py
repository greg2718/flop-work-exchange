"""Resolve stub vs local sibling adapters from AdapterConfig.

Stubs remain the default. Local adapters fail closed when backends are missing;
they never silently become stubs while still being labeled live.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from flop_work_exchange.adapters.bench import LocalBenchAdapter, StubBenchAdapter
from flop_work_exchange.adapters.router import LocalRouterAdapter, StubRouterAdapter
from flop_work_exchange.adapters.scout import LocalScoutAdapter, StubScoutAdapter
from flop_work_exchange.adapters.sentinel import LocalSentinelAdapter, StubSentinelAdapter
from flop_work_exchange.config import AdapterConfig
from flop_work_exchange.constants import (
    LEGACY_SCOUT_STATE,
    MAC_BENCH_REPO,
    MAC_ROUTER_REPO,
    MAC_SCOUT_REPO,
    MAC_SENTINEL_REPO,
)


@dataclass(frozen=True)
class AdapterBundle:
    scout: StubScoutAdapter | LocalScoutAdapter
    bench: StubBenchAdapter | LocalBenchAdapter
    router: StubRouterAdapter | LocalRouterAdapter
    sentinel: StubSentinelAdapter | LocalSentinelAdapter


def resolve_adapters(config: AdapterConfig) -> AdapterBundle:
    return AdapterBundle(
        scout=_resolve_scout(config),
        bench=_resolve_bench(config),
        router=_resolve_router(config),
        sentinel=_resolve_sentinel(config),
    )


def _resolve_scout(config: AdapterConfig) -> StubScoutAdapter | LocalScoutAdapter:
    if config.scout_mode != "local":
        return StubScoutAdapter()
    script = first_existing_file(
        config.scout_script,
        (config.scout_repo / "flop_scout.py") if config.scout_repo else None,
        MAC_SCOUT_REPO / "flop_scout.py",
    )
    state_dir = config.scout_state_dir
    if state_dir is None and LEGACY_SCOUT_STATE.is_dir():
        state_dir = LEGACY_SCOUT_STATE
    db_path = config.scout_db
    if db_path is None and state_dir is not None:
        db_path = state_dir / "observer.sqlite"
    return LocalScoutAdapter(
        script=script,
        python=config.python,
        state_dir=state_dir,
        db_path=db_path,
        evidence_jsonl=config.scout_evidence_jsonl,
        timeout_seconds=config.timeout_seconds,
    )


def _resolve_bench(config: AdapterConfig) -> StubBenchAdapter | LocalBenchAdapter:
    if config.bench_mode != "local":
        return StubBenchAdapter()
    return LocalBenchAdapter(
        argv=resolve_bench_argv(config),
        allow_local_exec=config.bench_allow_local_exec,
        timeout_seconds=config.timeout_seconds,
    )


def _resolve_router(config: AdapterConfig) -> StubRouterAdapter | LocalRouterAdapter:
    if config.router_mode != "local":
        return StubRouterAdapter()
    script = first_existing_file(
        config.router_script,
        (config.router_repo / "router.py") if config.router_repo else None,
        MAC_ROUTER_REPO / "router.py",
    )
    cwd = config.router_repo or (script.parent if script is not None else None)
    return LocalRouterAdapter(
        script=script,
        python=config.python,
        db_path=config.router_db,
        cwd=cwd,
        timeout_seconds=config.timeout_seconds,
    )


def _resolve_sentinel(config: AdapterConfig) -> StubSentinelAdapter | LocalSentinelAdapter:
    if config.sentinel_mode != "local":
        return StubSentinelAdapter()
    path = config.sentinel_path
    if path is None and MAC_SENTINEL_REPO.exists():
        path = MAC_SENTINEL_REPO
    return LocalSentinelAdapter(module_path=path)


def resolve_bench_argv(config: AdapterConfig) -> list[str] | None:
    if config.bench_cli:
        return _split_cli(config.bench_cli)
    which = shutil.which("flop-bench")
    if which:
        return [which]
    repos = [path for path in (config.bench_repo, MAC_BENCH_REPO) if path is not None]
    for repo in repos:
        venv_cli = repo.expanduser() / ".venv" / "bin" / "flop-bench"
        if venv_cli.is_file():
            return [str(venv_cli)]
    try:
        __import__("flop_bench")
    except ImportError:
        return None
    return [sys.executable, "-m", "flop_bench.cli"]


def first_existing_file(*candidates: Path | None) -> Path | None:
    for candidate in candidates:
        if candidate is not None and candidate.expanduser().is_file():
            return candidate.expanduser()
    return None


def _split_cli(value: str) -> list[str]:
    parts = value.split()
    return parts or [value]
