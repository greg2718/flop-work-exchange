from __future__ import annotations

from pathlib import Path

from flop_work_exchange.exceptions import IsolationError, SafetyError

DEFAULT_PRODUCTION_STATE = Path.home() / ".flop_agents" / "work-exchange"
EXCHANGE_OPERATOR_GROUP = "local-flop-agent-family"

SCOUT_DID = "did:key:z6MkfJnczowbivU9SEDcZ77MEpKUfQTVbcD3i1gcwsfo4yL1"
BENCH_DID = "did:key:z6MkqqqEMxujBTEAvoanSx6pVBMMZzLP7gMUcmNVdYHS3BVk"
ROUTER_DID = "did:key:z6MkpGs1L6fYEsaXsDfyDfrTxbKVeZ3evuPaBj2x38KzupPd"
SENTINEL_DID = "UNKNOWN_NOT_PROVISIONED"

SCOUT_STATE = Path.home() / ".flop_agents" / "scout"
BENCH_STATE = Path.home() / ".flop_agents" / "bench"
ROUTER_STATE = Path.home() / ".flop_agents" / "router"
SENTINEL_STATE = Path.home() / ".flop_agents" / "sentinel"
LEGACY_SCOUT_STATE = Path.home() / ".flop_scout"

KNOWN_FAMILY_AGENTS: tuple[dict[str, str], ...] = (
    {"name": "FLOP Scout", "did": SCOUT_DID},
    {"name": "FLOP Bench", "did": BENCH_DID},
    {"name": "FLOP Router", "did": ROUTER_DID},
    {"name": "FLOP Sentinel", "did": SENTINEL_DID},
)

KNOWN_FAMILY_DIDS: frozenset[str] = frozenset(
    agent["did"] for agent in KNOWN_FAMILY_AGENTS if agent["did"].startswith("did:")
)

FORBIDDEN_PAYMENT_PHRASES: tuple[str, ...] = (
    "faucet claim",
    "claimed faucet",
    "faucet drip",
    "room faucet",
    "airdrop code",
    "claim code",
    "seed phrase",
    "mnemonic",
    "private key",
    "wallet seed",
)


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def _path_overlaps(left: Path, right: Path) -> bool:
    resolved_left = left.expanduser().resolve(strict=False)
    resolved_right = right.expanduser().resolve(strict=False)
    if resolved_left == resolved_right:
        return True
    try:
        return resolved_left.is_relative_to(resolved_right) or resolved_right.is_relative_to(
            resolved_left
        )
    except ValueError:
        return False


def sibling_state_dirs() -> tuple[Path, ...]:
    return (SCOUT_STATE, BENCH_STATE, ROUTER_STATE, SENTINEL_STATE, LEGACY_SCOUT_STATE)


def assert_isolated_state_dir(state_dir: Path) -> Path:
    resolved = state_dir.expanduser().resolve(strict=False)
    if state_dir.expanduser().is_symlink() or resolved.is_symlink():
        raise IsolationError("state directory must not be a symlink")
    for sibling in sibling_state_dirs():
        if _path_overlaps(resolved, sibling):
            raise IsolationError(f"state directory overlaps sibling agent state: {sibling}")
    return resolved


def assert_not_production_auto_init(state_dir: Path) -> None:
    if _same_path(state_dir, DEFAULT_PRODUCTION_STATE):
        raise SafetyError(
            "refusing to auto-create identity in the production state directory; "
            "run identity init with explicit confirmation"
        )
