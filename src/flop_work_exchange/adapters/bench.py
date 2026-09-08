from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from flop_work_exchange.adapters.process import CommandResult, run_argv
from flop_work_exchange.canonical import result_hash_for, sha256_json
from flop_work_exchange.exceptions import AdapterError
from flop_work_exchange.models import BenchVerdict, Job

CommandRunner = Callable[..., CommandResult]
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class StubBenchAdapter:
    """Offline Bench adapter.

    Real Bench verifies test specs into hash-chained evidence bundles via
    ``flop-bench verify --state-dir ...`` and keeps local exec behind
    ``--allow-local-exec``. This stub never executes local commands. It checks
    that a submitted result hash matches the result payload.
    """

    kind = "stub"

    def probe(self) -> dict[str, Any]:
        return {
            "ok": True,
            "kind": self.kind,
            "note": "offline hash check; no flop-bench process; not independent Bench reputation",
        }

    def verify_delivery(self, job: Job) -> BenchVerdict:
        if not job.result_text or not job.result_hash:
            return BenchVerdict(
                result="FAIL",
                evidence_id=None,
                notes="missing result_text or result_hash",
            )
        expected = result_hash_for(job.result_text)
        if job.result_hash != expected:
            return BenchVerdict(
                result="FAIL",
                evidence_id=None,
                notes="result_hash does not match sha256(result_text)",
            )
        evidence_id = (
            "ev-"
            + sha256_json(
                {
                    "job_id": job.job_id,
                    "result_hash": job.result_hash,
                    "adapter": "stub-bench",
                }
            )[:32]
        )
        return BenchVerdict(
            result="PASS",
            evidence_id=evidence_id,
            notes="stub hash check only; no local exec; not independent Bench reputation",
            local_exec=False,
        )


class LocalBenchAdapter:
    """Invoke ``flop-bench verify`` on a generated passive spec.

    Always uses an isolated temporary ``--state-dir`` (never
    ``~/.flop_agents/bench``). ``--allow-local-exec`` is omitted unless
    ``allow_local_exec=True`` (default false). Same-operator Bench results are
    not independent reputation.
    """

    kind = "local"

    def __init__(
        self,
        *,
        argv: Sequence[str] | None = None,
        allow_local_exec: bool = False,
        timeout_seconds: float = 60.0,
        run_command: CommandRunner | None = None,
    ) -> None:
        self.argv = [str(part) for part in argv] if argv else None
        self.allow_local_exec = allow_local_exec
        self.timeout_seconds = timeout_seconds
        self._run_command = run_command or run_argv

    def probe(self) -> dict[str, Any]:
        try:
            argv = self._resolved_argv()
        except AdapterError as exc:
            return {"ok": False, "kind": self.kind, "error": str(exc), "allow_local_exec": False}
        return {
            "ok": True,
            "kind": self.kind,
            "argv": argv,
            "allow_local_exec": self.allow_local_exec,
            "note": "passive flop-bench verify; temp --state-dir; not independent reputation",
        }

    def verify_delivery(self, job: Job) -> BenchVerdict:
        if not job.result_text or not job.result_hash:
            return BenchVerdict(
                result="FAIL",
                evidence_id=None,
                notes="missing result_text or result_hash",
            )
        digest = _hex_digest(job.result_hash)
        argv_prefix = self._resolved_argv()
        with tempfile.TemporaryDirectory(prefix="flop-wx-bench-") as raw_tmp:
            tmp = Path(raw_tmp)
            artifact = tmp / "result.txt"
            spec_path = tmp / "spec.json"
            bench_state = tmp / "bench-state"
            artifact.write_text(job.result_text, encoding="utf-8")
            spec_path.write_text(
                json.dumps(passive_delivery_spec(job, artifact, digest), indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            command = [
                *argv_prefix,
                "verify",
                str(spec_path),
                "--state-dir",
                str(bench_state),
            ]
            if self.allow_local_exec:
                command.append("--allow-local-exec")
            result = self._run_command(command, timeout_seconds=self.timeout_seconds)
            return _verdict_from_bench_output(result)

    def _resolved_argv(self) -> list[str]:
        if self.argv:
            return list(self.argv)
        raise AdapterError(
            "LocalBenchAdapter: flop-bench CLI missing. Set FLOP_WX_BENCH_CLI or "
            "FLOP_WX_BENCH_REPO (expected `flop-bench verify --state-dir ...`)."
        )


def passive_delivery_spec(job: Job, artifact: Path, sha256_hex: str) -> dict[str, Any]:
    """Minimal flop-bench.test-spec.v0.1 passive spec for a job result artifact."""
    return {
        "schema_version": "flop-bench.test-spec.v0.1",
        "claim_id": job.job_id,
        "hypothesis": (
            f"Work Exchange job {job.job_id} result artifact matches the recorded sha256."
        ),
        "requested_capabilities": [job.service or "work-exchange-delivery"],
        "mode": "passive",
        "procedure": [
            {"adapter": "file_exists", "path": str(artifact)},
            {"adapter": "file_sha256", "path": str(artifact), "sha256": sha256_hex},
        ],
        "assertions": [{"expect": "result artifact exists and matches result_hash"}],
        "failure_conditions": [
            "result artifact missing",
            "result artifact sha256 does not match job.result_hash",
        ],
        "provenance": {
            "source": "flop-work-exchange",
            "job_id": job.job_id,
            "note": (
                "same-operator Bench verification is not independent peer reputation "
                "or independent fee volume"
            ),
        },
    }


def _hex_digest(result_hash: str) -> str:
    value = result_hash.removeprefix("sha256:")
    if not _SHA256_HEX.fullmatch(value):
        raise AdapterError("result_hash is not a 64-hex sha256 digest")
    return value


def _verdict_from_bench_output(result: CommandResult) -> BenchVerdict:
    payload = _parse_json_object(result.stdout) if result.stdout.strip() else None
    if payload is None:
        raise AdapterError(
            "flop-bench verify did not return JSON "
            f"(exit {result.returncode}): {_brief(result.stderr or result.stdout)}"
        )
    raw_result = str(payload.get("result") or "")
    if raw_result not in {"PASS", "FAIL", "PARTIAL"}:
        raise AdapterError(f"flop-bench verify returned unknown result: {raw_result!r}")
    evidence_id = payload.get("evidence_id")
    safety = payload.get("safety_report") if isinstance(payload.get("safety_report"), dict) else {}
    local_exec = bool(safety.get("local_execution")) if isinstance(safety, dict) else False
    notes = (
        "flop-bench verify; not independent Bench reputation; "
        f"local_exec={local_exec}; allow_local_exec_flag="
        f"{'--allow-local-exec' in result.argv}"
    )
    if result.returncode != 0 and raw_result == "PASS":
        raise AdapterError(
            f"flop-bench verify exit {result.returncode} but JSON result=PASS; failing closed"
        )
    return BenchVerdict(
        result=raw_result,  # type: ignore[arg-type]
        evidence_id=str(evidence_id) if evidence_id else None,
        notes=notes,
        local_exec=local_exec,
    )


def _parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            loaded = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return loaded if isinstance(loaded, dict) else None


def _brief(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "…"
