from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from flop_work_exchange.adapters.process import CommandResult, run_argv
from flop_work_exchange.constants import (
    DEFAULT_SCOUT_CANDIDATE_LIMIT,
    EXCHANGE_OPERATOR_GROUP,
    KNOWN_FAMILY_DIDS,
    MAX_EVIDENCE_IDS_PER_CANDIDATE,
    SCOUT_EVIDENCE_FEED_CLI,
)
from flop_work_exchange.exceptions import AdapterError
from flop_work_exchange.identity import is_valid_ed25519_did
from flop_work_exchange.models import Job, WorkerCandidate

CommandRunner = Callable[..., CommandResult]


def candidates_from_records(
    records: Iterable[Mapping[str, Any]],
    *,
    source: str,
    notes: str,
    limit: int = DEFAULT_SCOUT_CANDIDATE_LIMIT,
) -> list[WorkerCandidate]:
    """Map Scout-shaped evidence records to worker candidates.

    Record text is hostile/untrusted and is never copied into notes.
    Ranked by evidence count (desc), then DID. Output is capped.
    """
    grouped: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for line_no, record in enumerate(records, start=1):
        did = record.get("did") or record.get("sender_did")
        if not isinstance(did, str) or not is_valid_ed25519_did(did):
            continue
        evidence_id = str(record.get("evidence_id") or record.get("id") or f"line-{line_no}")
        ids = grouped.setdefault(did, [])
        if evidence_id not in ids:
            ids.append(evidence_id)
        hint = record.get("evidence_count")
        if isinstance(hint, int) and hint > counts.get(did, 0):
            counts[did] = hint
    for did, ids in grouped.items():
        counts.setdefault(did, len(ids))
    ranked = sorted(grouped.items(), key=lambda item: (-counts[item[0]], item[0]))
    cap = max(1, limit)
    candidates: list[WorkerCandidate] = []
    for did, evidence_ids in ranked[:cap]:
        operator_group = EXCHANGE_OPERATOR_GROUP if did in KNOWN_FAMILY_DIDS else None
        candidates.append(
            WorkerCandidate(
                did=did,
                evidence_ids=evidence_ids[:MAX_EVIDENCE_IDS_PER_CANDIDATE],
                notes=notes,
                operator_group=operator_group,
                source=source,
            )
        )
    return candidates


def cap_worker_candidates(
    candidates: Iterable[WorkerCandidate],
    limit: int = DEFAULT_SCOUT_CANDIDATE_LIMIT,
) -> list[WorkerCandidate]:
    """Keep top-N candidates. Preserve adapter order when already within the cap."""
    items = list(candidates)
    cap = max(1, limit)
    if len(items) > cap:
        items = sorted(items, key=lambda item: (-len(item.evidence_ids), item.did))[:cap]
    out: list[WorkerCandidate] = []
    for item in items[:cap]:
        out.append(
            WorkerCandidate(
                did=item.did,
                evidence_ids=list(item.evidence_ids)[:MAX_EVIDENCE_IDS_PER_CANDIDATE],
                notes=item.notes,
                operator_group=item.operator_group,
                source=item.source,
            )
        )
    return out


def candidates_payload(
    candidates: list[WorkerCandidate],
    *,
    limit: int,
    total: int | None = None,
) -> dict[str, Any]:
    shown = list(candidates)
    counted = total if total is not None else len(shown)
    return {
        "limit": limit,
        "shown": len(shown),
        "truncated": counted > len(shown),
        "candidates": [
            {
                "did": item.did,
                "evidence_ids": list(item.evidence_ids)[:MAX_EVIDENCE_IDS_PER_CANDIDATE],
                "notes": item.notes,
                "operator_group": item.operator_group,
                "source": item.source,
            }
            for item in shown
        ],
    }


class StubScoutAdapter:
    """Offline Scout adapter.

    Later wiring can invoke Scout's evidence feed:

        python flop_scout.py evidence feed --since-id 0 --format jsonl

    This stub never opens a network connection. If ``evidence_path`` is set, it
    reads local JSONL records and collects ``did`` fields as candidates.
    """

    kind = "stub"

    def __init__(
        self,
        evidence_path: Path | None = None,
        extra_dids: list[str] | None = None,
        *,
        candidate_limit: int = DEFAULT_SCOUT_CANDIDATE_LIMIT,
    ) -> None:
        self.evidence_path = evidence_path
        self.extra_dids = extra_dids or []
        self.candidate_limit = candidate_limit

    def probe(self) -> dict[str, Any]:
        return {
            "ok": True,
            "kind": self.kind,
            "note": "offline default; not a live Scout backend",
            "candidate_limit": self.candidate_limit,
        }

    def find_candidates(self, job: Job) -> list[WorkerCandidate]:
        del job
        candidates: list[WorkerCandidate] = []
        seen: set[str] = set()
        for did in self.extra_dids:
            if did not in seen and is_valid_ed25519_did(did):
                seen.add(did)
                candidates.append(
                    WorkerCandidate(did=did, notes="configured stub candidate", source="stub")
                )
        if self.evidence_path and self.evidence_path.exists():
            records = _read_jsonl_records(self.evidence_path)
            for candidate in candidates_from_records(
                records,
                source="evidence-jsonl",
                notes="local evidence feed stub",
                limit=self.candidate_limit,
            ):
                if candidate.did not in seen:
                    seen.add(candidate.did)
                    candidates.append(candidate)
        return cap_worker_candidates(candidates, self.candidate_limit)


class LocalScoutAdapter:
    """Local Scout adapter. Reads a local evidence feed or observer DB only.

    Expected CLI (not yet in Scout v0.3.3; tried first when a script is configured):

        python flop_scout.py evidence feed --since-id 0 --format jsonl

    Fallback: read-only ``evidence_records`` in the local observer SQLite DB
    (``~/.flop_scout/observer.sqlite`` or ``FLOP_SCOUT_STATE_DIR``). Never runs
    ``observe`` / ``read`` / ``say`` (those hit the network). Fail closed if no
    local backend is present — this adapter never silently becomes the stub.
    """

    kind = "local"

    def __init__(
        self,
        *,
        script: Path | None = None,
        python: str = "python3",
        state_dir: Path | None = None,
        db_path: Path | None = None,
        evidence_jsonl: Path | None = None,
        timeout_seconds: float = 30.0,
        run_command: CommandRunner | None = None,
        candidate_limit: int = DEFAULT_SCOUT_CANDIDATE_LIMIT,
    ) -> None:
        self.script = script.expanduser() if script is not None else None
        self.python = python
        self.state_dir = state_dir.expanduser() if state_dir is not None else None
        self.db_path = db_path.expanduser() if db_path is not None else None
        self.evidence_jsonl = evidence_jsonl.expanduser() if evidence_jsonl is not None else None
        self.timeout_seconds = timeout_seconds
        self._run_command = run_command or run_argv
        self.candidate_limit = candidate_limit

    def probe(self) -> dict[str, Any]:
        sources = self._available_sources()
        ok = bool(sources)
        return {
            "ok": ok,
            "kind": self.kind,
            "cli_contract": SCOUT_EVIDENCE_FEED_CLI,
            "script": str(self.script) if self.script else None,
            "db_path": str(self._resolved_db_path()) if self._resolved_db_path() else None,
            "evidence_jsonl": str(self.evidence_jsonl) if self.evidence_jsonl else None,
            "candidate_limit": self.candidate_limit,
            "sources": sources,
            "error": None if ok else "Scout local backend missing (script, observer DB, or JSONL)",
        }

    def find_candidates(self, job: Job) -> list[WorkerCandidate]:
        del job
        records, source = self._load_records()
        return candidates_from_records(
            records,
            source=source,
            notes="local scout evidence; message text omitted (untrusted)",
            limit=self.candidate_limit,
        )

    def _available_sources(self) -> list[str]:
        sources: list[str] = []
        if self.script is not None and self.script.is_file():
            sources.append(f"script:{self.script}")
        db_path = self._resolved_db_path()
        if db_path is not None and db_path.is_file():
            sources.append(f"sqlite:{db_path}")
        if self.evidence_jsonl is not None and self.evidence_jsonl.is_file():
            sources.append(f"jsonl:{self.evidence_jsonl}")
        return sources

    def _resolved_db_path(self) -> Path | None:
        if self.db_path is not None:
            return self.db_path
        if self.state_dir is not None:
            return self.state_dir / "observer.sqlite"
        return None

    def _load_records(self) -> tuple[list[dict[str, Any]], str]:
        if self.script is not None and self.script.is_file():
            try:
                return self._records_from_feed_cli(), "local-scout-feed"
            except AdapterError:
                # Scout v0.3.3 has no `evidence feed` yet; fall through to local DB.
                pass
        db_path = self._resolved_db_path()
        if db_path is not None and db_path.is_file():
            return self._records_from_sqlite(db_path), "local-scout-sqlite"
        if self.evidence_jsonl is not None and self.evidence_jsonl.is_file():
            return _read_jsonl_records(self.evidence_jsonl), "local-scout-jsonl"
        raise AdapterError(
            "LocalScoutAdapter: Scout backend missing. Configure FLOP_WX_SCOUT_SCRIPT, "
            "FLOP_WX_SCOUT_DB / FLOP_SCOUT_STATE_DIR, or FLOP_WX_SCOUT_EVIDENCE_JSONL. "
            f"Expected CLI: {SCOUT_EVIDENCE_FEED_CLI}"
        )

    def _records_from_feed_cli(self) -> list[dict[str, Any]]:
        assert self.script is not None
        extra_env: dict[str, str] = {}
        if self.state_dir is not None:
            extra_env["FLOP_SCOUT_STATE_DIR"] = str(self.state_dir)
        result = self._run_command(
            [
                self.python,
                str(self.script),
                "evidence",
                "feed",
                "--since-id",
                "0",
                "--format",
                "jsonl",
            ],
            timeout_seconds=self.timeout_seconds,
            extra_env=extra_env or None,
        )
        if result.returncode != 0:
            raise AdapterError(
                "Scout evidence feed failed "
                f"(exit {result.returncode}): {_brief(result.stderr or result.stdout)}"
            )
        return _parse_jsonl_text(result.stdout)

    def _records_from_sqlite(self, db_path: Path) -> list[dict[str, Any]]:
        uri = f"file:{db_path.resolve()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise AdapterError(
                f"Scout observer DB could not be opened read-only: {db_path}"
            ) from exc
        conn.row_factory = sqlite3.Row
        try:
            try:
                ranked = conn.execute(
                    """
                    SELECT did, COUNT(*) AS evidence_count
                    FROM evidence_records
                    WHERE did IS NOT NULL AND did != ''
                    GROUP BY did
                    ORDER BY evidence_count DESC, did ASC
                    LIMIT ?
                    """,
                    (self.candidate_limit,),
                ).fetchall()
            except sqlite3.Error as exc:
                raise AdapterError(
                    f"Scout observer DB has no readable evidence_records table: {db_path}"
                ) from exc
            records: list[dict[str, Any]] = []
            for row in ranked:
                did = row["did"]
                try:
                    evid_rows = conn.execute(
                        """
                        SELECT evidence_id
                        FROM evidence_records
                        WHERE did = ?
                        ORDER BY retrieved_at DESC, evidence_id DESC
                        LIMIT ?
                        """,
                        (did, MAX_EVIDENCE_IDS_PER_CANDIDATE),
                    ).fetchall()
                except sqlite3.Error:
                    evid_rows = []
                if evid_rows:
                    for evid in evid_rows:
                        records.append(
                            {
                                "did": did,
                                "evidence_id": evid["evidence_id"],
                                "evidence_count": int(row["evidence_count"]),
                            }
                        )
                else:
                    records.append(
                        {
                            "did": did,
                            "evidence_id": f"count-{row['evidence_count']}",
                            "evidence_count": int(row["evidence_count"]),
                        }
                    )
        finally:
            conn.close()
        return records


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    return _parse_jsonl_text(path.read_text(encoding="utf-8"), label=str(path))


def _parse_jsonl_text(text: str, *, label: str = "scout feed") -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"Scout evidence JSONL invalid at {label} line {line_no}") from exc
        if not isinstance(record, dict):
            raise AdapterError(f"Scout evidence JSONL must be objects at {label} line {line_no}")
        records.append(record)
    return records


def _brief(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "…"
