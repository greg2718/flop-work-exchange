"""Subprocess helper for local sibling-agent adapters.

Commands are argv lists only (no shell). Adapters must not pass wallet
secrets, identity passphrases, or Scout/Bench/Router/Sentinel private keys.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from flop_work_exchange.exceptions import AdapterError


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def run_argv(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    cwd: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> CommandResult:
    if not argv:
        raise AdapterError("empty sibling command")
    env = os.environ.copy()
    if extra_env:
        env.update(dict(extra_env))
    try:
        completed = subprocess.run(  # noqa: S603
            [str(part) for part in argv],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
        )
    except FileNotFoundError as exc:
        raise AdapterError(f"command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdapterError(f"command timed out after {timeout_seconds}s: {argv[0]}") from exc
    return CommandResult(
        argv=tuple(str(part) for part in argv),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
