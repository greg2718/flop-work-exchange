from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from flop_work_exchange.adapters.process import CommandResult, run_argv
from flop_work_exchange.constants import (
    DEFAULT_SCOUT_CANDIDATE_LIMIT,
    DEFAULT_SCOUT_SQLITE_TIMEOUT_SECONDS,
    EXCHANGE_OPERATOR_GROUP,
    KNOWN_FAMILY_DIDS,
    MAX_EVIDENCE_IDS_PER_CANDIDATE,
    SCOUT_EVIDENCE_FEED_CLI,
    SCOUT_MAX_QUERY_DB_BYTES,
    SCOUT_WAREHOUSE_DB_NAME,
)
from flop_work_exchange.exceptions import AdapterError
from flop_work_exchange.identity import is_valid_ed25519_did
from flop_work_exchange.models import Job, WorkerCandidate

CommandRunner = Callable[..., CommandResult]
T = TypeVar("T")


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

    Preference order:

    1. Configured evidence JSONL (``scout_evidence_jsonl``)
    2. Scout projection SQLite (``scout_projection_db``, ≤1GiB by default)
    3. CLI ``python flop_scout.py evidence feed --since-id 0 --format jsonl``
       (timeout_seconds; Scout v0.3.3 may not implement ``feed`` yet)
    4. Raw observer warehouse (``observer.sqlite``) only when it is ≤ the size
       cap, with a hard sqlite wall-clock timeout

    Oversized warehouses (~48–52GiB ``observer.sqlite``) are never scanned.
    ``GROUP BY did`` over the full table would hang even with ``LIMIT``. Fail
    closed with ``AdapterError`` so live-demo can fall back to the stub.

    Never runs ``observe`` / ``read`` / ``say`` (those hit the network). This
    adapter never silently becomes the stub while still labeled live.
    """

    kind = "local"

    def __init__(
        self,
        *,
        script: Path | None = None,
        python: str = "python3",
        state_dir: Path | None = None,
        db_path: Path | None = None,
        projection_db: Path | None = None,
        evidence_jsonl: Path | None = None,
        timeout_seconds: float = 30.0,
        sqlite_timeout_seconds: float = DEFAULT_SCOUT_SQLITE_TIMEOUT_SECONDS,
        max_db_bytes: int = SCOUT_MAX_QUERY_DB_BYTES,
        run_command: CommandRunner | None = None,
        candidate_limit: int = DEFAULT_SCOUT_CANDIDATE_LIMIT,
    ) -> None:
        self.script = script.expanduser() if script is not None else None
        self.python = python
        self.state_dir = state_dir.expanduser() if state_dir is not None else None
        self.db_path = db_path.expanduser() if db_path is not None else None
        self.projection_db = projection_db.expanduser() if projection_db is not None else None
        self.evidence_jsonl = evidence_jsonl.expanduser() if evidence_jsonl is not None else None
        self.timeout_seconds = timeout_seconds
        self.sqlite_timeout_seconds = sqlite_timeout_seconds
        self.max_db_bytes = max_db_bytes
        self._run_command = run_command or run_argv
        self.candidate_limit = candidate_limit

    def probe(self) -> dict[str, Any]:
        plan = self._source_plan()
        return {
            "ok": plan["ok"],
            "kind": self.kind,
            "cli_contract": SCOUT_EVIDENCE_FEED_CLI,
            "script": str(self.script) if self.script else None,
            "db_path": str(self._resolved_db_path()) if self._resolved_db_path() else None,
            "projection_db": str(self.projection_db) if self.projection_db else None,
            "evidence_jsonl": str(self.evidence_jsonl) if self.evidence_jsonl else None,
            "candidate_limit": self.candidate_limit,
            "sqlite_timeout_seconds": self.sqlite_timeout_seconds,
            "max_db_bytes": self.max_db_bytes,
            "preferred_source": plan["preferred_source"],
            "sources": plan["sources"],
            "warehouse": plan["warehouse"],
            "projection": plan["projection"],
            "error": None if plan["ok"] else plan["error"],
            "note": plan.get("note"),
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

    def _source_plan(self) -> dict[str, Any]:
        sources: list[str] = []
        preferred: str | None = None

        def add(kind: str, label: str) -> None:
            nonlocal preferred
            sources.append(label)
            if preferred is None:
                preferred = kind

        if self.evidence_jsonl is not None and self.evidence_jsonl.is_file():
            add("jsonl", f"jsonl:{self.evidence_jsonl}")
        projection = assess_scout_sqlite(
            self.projection_db, max_bytes=self.max_db_bytes, role="projection"
        )
        if projection.get("ok"):
            add("projection", f"projection:{projection['path']}")
        if self.script is not None and self.script.is_file():
            add("cli", f"script:{self.script}")
        warehouse = assess_scout_sqlite(
            self._resolved_db_path(), max_bytes=self.max_db_bytes, role="warehouse"
        )
        if warehouse.get("ok"):
            add("sqlite", f"sqlite:{warehouse['path']}")
        note = None
        error = None
        if warehouse.get("oversized"):
            note = str(warehouse.get("error") or "")
        if not sources:
            error = note or (
                "Scout local backend missing (JSONL, projection ≤1GiB, script, "
                "or observer DB under the size cap)"
            )
        return {
            "ok": bool(sources),
            "preferred_source": preferred,
            "sources": sources,
            "warehouse": warehouse,
            "projection": projection,
            "error": error,
            "note": note,
        }

    def _resolved_db_path(self) -> Path | None:
        if self.db_path is not None:
            return self.db_path
        if self.state_dir is not None:
            return self.state_dir / SCOUT_WAREHOUSE_DB_NAME
        return None

    def _load_records(self) -> tuple[list[dict[str, Any]], str]:
        errors: list[str] = []
        if self.evidence_jsonl is not None and self.evidence_jsonl.is_file():
            return _read_jsonl_records(self.evidence_jsonl), "local-scout-jsonl"

        projection = assess_scout_sqlite(
            self.projection_db, max_bytes=self.max_db_bytes, role="projection"
        )
        if projection.get("ok"):
            try:
                return (
                    self._records_from_sqlite(Path(str(projection["path"]))),
                    "local-scout-projection",
                )
            except AdapterError as exc:
                errors.append(str(exc))
        elif self.projection_db is not None:
            errors.append(
                str(projection.get("error") or f"projection unusable: {self.projection_db}")
            )

        if self.script is not None and self.script.is_file():
            try:
                return self._records_from_feed_cli(), "local-scout-feed"
            except AdapterError as exc:
                # Scout v0.3.3 has no `evidence feed` yet; fall through.
                errors.append(str(exc))

        warehouse = assess_scout_sqlite(
            self._resolved_db_path(), max_bytes=self.max_db_bytes, role="warehouse"
        )
        if warehouse.get("ok"):
            try:
                return (
                    self._records_from_sqlite(Path(str(warehouse["path"]))),
                    "local-scout-sqlite",
                )
            except AdapterError as exc:
                errors.append(str(exc))
        elif self._resolved_db_path() is not None:
            errors.append(str(warehouse.get("error") or "Scout sqlite warehouse unusable"))

        detail = f" ({'; '.join(errors)})" if errors else ""
        raise AdapterError(
            "LocalScoutAdapter: Scout backend missing. Configure "
            "FLOP_WX_SCOUT_EVIDENCE_JSONL, FLOP_WX_SCOUT_PROJECTION_DB (≤1GiB), "
            "FLOP_WX_SCOUT_SCRIPT, or a small FLOP_WX_SCOUT_DB. "
            "Raw observer.sqlite warehouses are not queried. "
            f"Expected CLI: {SCOUT_EVIDENCE_FEED_CLI}{detail}"
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
        limit = self.candidate_limit
        id_cap = MAX_EVIDENCE_IDS_PER_CANDIDATE

        def query(conn: sqlite3.Connection) -> list[dict[str, Any]]:
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
                    (limit,),
                ).fetchall()
            except sqlite3.Error as exc:
                if _is_sqlite_interrupt(exc):
                    raise
                raise AdapterError(
                    f"Scout sqlite DB has no readable evidence_records table: {db_path}"
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
                        (did, id_cap),
                    ).fetchall()
                except sqlite3.Error as exc:
                    if _is_sqlite_interrupt(exc):
                        raise
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
            return records

        return run_sqlite_readonly(
            db_path, query, timeout_seconds=self.sqlite_timeout_seconds
        )


def assess_scout_sqlite(
    db_path: Path | None,
    *,
    max_bytes: int = SCOUT_MAX_QUERY_DB_BYTES,
    role: str = "sqlite",
) -> dict[str, Any]:
    """Classify a Scout sqlite file as usable, missing, or oversized warehouse.

    Size is taken from ``stat`` only. The raw observer warehouse is not opened.
    """
    if db_path is None:
        return {
            "ok": False,
            "role": role,
            "path": None,
            "bytes": None,
            "oversized": False,
            "risky": False,
            "usable": False,
            "error": f"scout {role} db is not set",
        }
    path = db_path.expanduser()
    if not path.is_file():
        return {
            "ok": False,
            "role": role,
            "path": str(path),
            "bytes": None,
            "oversized": False,
            "risky": False,
            "usable": False,
            "error": f"scout {role} db not found: {path}",
        }
    size = path.stat().st_size
    warehouse_name = path.name == SCOUT_WAREHOUSE_DB_NAME
    oversized = size > max_bytes
    if oversized:
        gib = size / float(1024**3)
        cap_gib = max_bytes / float(1024**3)
        kind = "observer warehouse" if warehouse_name or role == "warehouse" else role
        return {
            "ok": False,
            "role": role,
            "path": str(path),
            "bytes": size,
            "oversized": True,
            "risky": True,
            "usable": False,
            "error": (
                f"Scout {kind} is {gib:.1f}GiB at {path}; max query size is {cap_gib:.1f}GiB. "
                "Work Exchange will not GROUP BY the raw warehouse. Use scout_evidence_jsonl "
                "or scout_projection_db (≤1GiB), not observer.sqlite."
            ),
        }
    return {
        "ok": True,
        "role": role,
        "path": str(path),
        "bytes": size,
        "oversized": False,
        "risky": False,
        "usable": True,
        "error": None,
    }


def run_sqlite_readonly(
    db_path: Path,
    callback: Callable[[sqlite3.Connection], T],
    *,
    timeout_seconds: float,
) -> T:
    """Open sqlite read-only and run ``callback`` with a wall-clock deadline.

    ``busy_timeout`` only covers lock waits. Long ``GROUP BY`` scans are aborted
    via ``set_progress_handler`` plus ``Connection.interrupt`` from a timer.
    """
    if timeout_seconds <= 0:
        raise AdapterError("Scout sqlite timeout_seconds must be > 0")
    uri = f"file:{db_path.resolve()}?mode=ro"
    busy_seconds = max(0.001, min(float(timeout_seconds), 5.0))
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=busy_seconds)
    except sqlite3.Error as exc:
        raise AdapterError(f"Scout sqlite DB could not be opened read-only: {db_path}") from exc
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic() + float(timeout_seconds)

    def on_progress() -> int:
        return 1 if time.monotonic() >= deadline else 0

    conn.set_progress_handler(on_progress, 64)
    timer = threading.Timer(float(timeout_seconds), conn.interrupt)
    timer.daemon = True
    timer.start()
    try:
        return callback(conn)
    except sqlite3.OperationalError as exc:
        if time.monotonic() >= deadline or _is_sqlite_interrupt(exc):
            raise AdapterError(
                f"Scout sqlite query timed out after {timeout_seconds}s ({db_path})"
            ) from exc
        raise AdapterError(f"Scout sqlite query failed ({db_path}): {exc}") from exc
    finally:
        timer.cancel()
        conn.set_progress_handler(None, 0)
        conn.close()


def _is_sqlite_interrupt(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "interrupt" in text or "interrupted" in text or "cancelled" in text


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
