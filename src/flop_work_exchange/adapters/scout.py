from __future__ import annotations

import json
from pathlib import Path

from flop_work_exchange.exceptions import AdapterError
from flop_work_exchange.identity import is_valid_ed25519_did
from flop_work_exchange.models import Job, WorkerCandidate


class StubScoutAdapter:
    """Offline Scout adapter.

    Later wiring can invoke Scout's evidence feed:

        python flop_scout.py evidence feed --since-id 0 --format jsonl

    This stub never opens a network connection. If ``evidence_path`` is set, it
    reads local JSONL records and collects ``did`` fields as candidates.
    """

    def __init__(
        self, evidence_path: Path | None = None, extra_dids: list[str] | None = None
    ) -> None:
        self.evidence_path = evidence_path
        self.extra_dids = extra_dids or []

    def find_candidates(self, job: Job) -> list[WorkerCandidate]:
        candidates: list[WorkerCandidate] = []
        seen: set[str] = set()
        for did in self.extra_dids:
            if did not in seen and is_valid_ed25519_did(did):
                seen.add(did)
                candidates.append(
                    WorkerCandidate(did=did, notes="configured stub candidate", source="stub")
                )
        if self.evidence_path and self.evidence_path.exists():
            for line_no, line in enumerate(
                self.evidence_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AdapterError(f"Scout evidence JSONL invalid at line {line_no}") from exc
                did = record.get("did") or record.get("sender_did")
                if isinstance(did, str) and is_valid_ed25519_did(did) and did not in seen:
                    seen.add(did)
                    evidence_id = str(
                        record.get("evidence_id") or record.get("id") or f"line-{line_no}"
                    )
                    candidates.append(
                        WorkerCandidate(
                            did=did,
                            evidence_ids=[evidence_id],
                            notes="local evidence feed stub",
                            source="evidence-jsonl",
                        )
                    )
        return candidates
