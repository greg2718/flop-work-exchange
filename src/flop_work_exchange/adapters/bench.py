from __future__ import annotations

from flop_work_exchange.canonical import result_hash_for, sha256_json
from flop_work_exchange.models import BenchVerdict, Job


class StubBenchAdapter:
    """Offline Bench adapter.

    Real Bench verifies test specs into hash-chained evidence bundles via
    ``flop-bench verify --state-dir ...`` and keeps local exec behind
    ``--allow-local-exec``. This stub never executes local commands. It checks
    that a submitted result hash matches the result payload.
    """

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
