from __future__ import annotations

import importlib
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

from flop_work_exchange.constants import EXCHANGE_OPERATOR_GROUP, KNOWN_FAMILY_DIDS
from flop_work_exchange.exceptions import AdapterError
from flop_work_exchange.identity import is_valid_ed25519_did
from flop_work_exchange.models import SentinelVerdict

PROMPT_INJECTION = re.compile(
    r"ignore (all|previous|prior) instructions|you are now|system prompt",
    re.IGNORECASE,
)
SECRET_REQUEST = re.compile(
    r"seed phrase|mnemonic|private key|identity\.pem|wallet seed|passphrase",
    re.IGNORECASE,
)
UNSAFE_EXEC = re.compile(
    r"rm\s+-rf|curl\s+[^\n]+\|\s*sh|wget\s+[^\n]+\|\s*sh|os\.system\(|subprocess\.call",
    re.IGNORECASE,
)
SUSPICIOUS_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
SYBIL = re.compile(r"bulk identit|sybil|generate dids", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_MAX_REASON = 200
_RULE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,80}$")
_DECISION_MAP = {
    "ALLOW": "ALLOW",
    "ALLOWED": "ALLOW",
    "PERMIT": "ALLOW",
    "PASS": "ALLOW",
    "OK": "ALLOW",
    "REJECT": "REJECT",
    "DENIED": "REJECT",
    "DENY": "REJECT",
    "BLOCK": "REJECT",
    "BLOCKED": "REJECT",
    "FAIL": "REJECT",
    "REVIEW": "REVIEW",
    "WARN": "REVIEW",
    "HOLD": "REVIEW",
    "ESCALATE": "REVIEW",
}
_HIGH_RISK = {"high", "critical", "severe", "reject"}
_MEDIUM_RISK = {"medium", "moderate", "review"}
_LOW_RISK = {"low", "info", "informational", "none", "allow"}

Importer = Callable[[], Any]


class StubSentinelAdapter:
    """Offline Sentinel-shaped adapter: artifact in, verdict out, fail-closed.

    This is not FLOP Sentinel. Real Sentinel is a pure deterministic library
    (stdlib + cryptography, no network). Swap this stub for a wrapper that
    calls Sentinel when that package is available. Signals match the family
    vocabulary so verdicts compose with Router SECURITY_POLICY.
    """

    kind = "stub"

    def __init__(self, *, seen_templates: set[str] | None = None) -> None:
        self.seen_templates = seen_templates if seen_templates is not None else set()

    def probe(self) -> dict[str, Any]:
        return {
            "ok": True,
            "kind": self.kind,
            "note": "offline regex screen; not flop_sentinel",
        }

    def screen(self, artifact_type: str, artifact: dict[str, Any]) -> SentinelVerdict:
        del artifact_type
        blob = json.dumps(artifact, sort_keys=True)
        signals: list[str] = []
        reasons: list[str] = []
        if PROMPT_INJECTION.search(blob):
            signals.append("prompt_injection")
            reasons.append("prompt-injection phrasing in artifact")
        if SECRET_REQUEST.search(blob):
            signals.append("secret_request")
            reasons.append("secret or key material requested")
        if UNSAFE_EXEC.search(blob):
            signals.append("unsafe_execution_request")
            reasons.append("unsafe execution request")
        if SUSPICIOUS_URL.search(blob):
            signals.append("suspicious_url")
            reasons.append("URL-like text treated as untrusted data")
        if SYBIL.search(blob):
            signals.append("sybil_signal")
            reasons.append("sybil / bulk-identity language")
        claimed = artifact.get("claimed_did")
        actual = artifact.get("sender_did")
        if claimed and actual and claimed != actual:
            signals.append("sender_identity_mismatch")
            reasons.append("claimed sender DID does not match sender_did")
        template = artifact.get("template_hash") or blob
        if isinstance(template, str) and template in self.seen_templates:
            signals.append("repeated_template")
            reasons.append("repeated template hash")
        elif isinstance(template, str):
            self.seen_templates.add(template)

        reject_signals = {
            "prompt_injection",
            "secret_request",
            "unsafe_execution_request",
            "sybil_signal",
            "sender_identity_mismatch",
        }
        if set(signals) & reject_signals:
            return SentinelVerdict(action="REJECT", signals=signals, reasons=reasons)
        if "suspicious_url" in signals or "repeated_template" in signals:
            # Fail-closed: URLs are hostile/untrusted data. Jobs may mention
            # them only as inert text; the stub reviews rather than allowing
            # automatic follow. Exchange orchestration still refuses REJECT.
            return SentinelVerdict(action="REVIEW", signals=signals, reasons=reasons)
        return SentinelVerdict(action="ALLOW", signals=signals, reasons=reasons or ["clean"])


class LocalSentinelAdapter:
    """Import flop_sentinel and map ``policy.decide`` onto SentinelVerdict.

    Real library contract (unpublished ``greg2718/flop-sentinel``):

        normalize artifact → run detectors → flop_sentinel.policy.decide(
            findings, provenance, affiliation, ...
        )

    Top-level ``decide(artifact_type, artifact)`` / ``screen`` are **not** the
    API. Verdict fields: ``risk``, ``decision``, ``signals``, ``findings``.
    Findings copied into Work Exchange contain **rule ids only** — never
    artifact/attacker text. Fail closed if the package or API is missing.
    """

    kind = "local"

    def __init__(
        self,
        *,
        module_path: Path | None = None,
        importer: Importer | None = None,
    ) -> None:
        self.module_path = module_path.expanduser() if module_path is not None else None
        self._importer = importer
        self._module: Any | None = None

    def probe(self) -> dict[str, Any]:
        try:
            module = self._load_module()
            _require_policy_decide(module)
            _require_detectors(module)
        except AdapterError as exc:
            return {"ok": False, "kind": self.kind, "error": str(exc)}
        return {
            "ok": True,
            "kind": self.kind,
            "module": getattr(module, "__name__", "flop_sentinel"),
            "api": "flop_sentinel.policy.decide(findings, provenance, affiliation, ...)",
            "note": "detectors + policy.decide; findings are rule ids only; no network",
        }

    def screen(self, artifact_type: str, artifact: dict[str, Any]) -> SentinelVerdict:
        module = self._load_module()
        decide = _require_policy_decide(module)
        normalized = normalize_sentinel_artifact(artifact_type, artifact)
        findings = collect_sentinel_findings(module, normalized)
        provenance = {
            "source": "flop-work-exchange",
            "artifact_type": artifact_type,
            "payment_mode": "paper",
            "settlement_execution": "DISABLED",
        }
        affiliation = _affiliation_from_artifact(artifact)
        raw = _invoke_policy_decide(decide, findings, provenance, affiliation)
        return verdict_from_sentinel_raw(raw)

    def _load_module(self) -> Any:
        if self._module is not None:
            return self._module
        if self._importer is not None:
            self._module = self._importer()
            return self._module
        _prepare_sys_path(self.module_path)
        try:
            self._module = importlib.import_module("flop_sentinel")
        except ImportError as exc:
            raise AdapterError(
                "LocalSentinelAdapter: flop_sentinel is not importable. Install a local "
                "checkout (`pip install -e ~/dev/flop_sentinel`) or set "
                f"FLOP_WX_SENTINEL_PATH. Import error: {exc}"
            ) from exc
        return self._module


def normalize_sentinel_artifact(artifact_type: str, artifact: dict[str, Any]) -> dict[str, Any]:
    """Structured detector input. Raw field text is for detection only."""
    text_fields = {
        str(key): str(value) for key, value in artifact.items() if isinstance(value, str)
    }
    return {
        "artifact_type": artifact_type,
        "field_names": sorted(str(key) for key in artifact.keys()),
        "dids": _extract_dids(artifact),
        "text_fields": text_fields,
        "artifact": dict(artifact),
    }


def collect_sentinel_findings(module: Any, normalized: dict[str, Any]) -> list[Any]:
    detectors = _require_detectors(module)
    for name in ("run", "detect", "collect", "run_all", "scan", "evaluate"):
        fn = getattr(detectors, name, None)
        if callable(fn):
            return _as_list(fn(normalized))
    for registry_name in ("DETECTORS", "ALL", "REGISTRY"):
        registry = getattr(detectors, registry_name, None)
        if registry:
            findings: list[Any] = []
            for item in registry:
                fn = item if callable(item) else getattr(item, "detect", None) or getattr(
                    item, "run", None
                )
                if callable(fn):
                    findings.extend(_as_list(fn(normalized)))
            return findings
    collected: list[Any] = []
    found_any = False
    for name in dir(detectors):
        if name.startswith("_"):
            continue
        obj = getattr(detectors, name)
        if callable(obj) and (name.startswith("detect") or name.endswith("_detector")):
            found_any = True
            collected.extend(_as_list(obj(normalized)))
    if found_any:
        return collected
    raise AdapterError(
        "flop_sentinel.detectors has no run/detect/DETECTORS entry point; fail closed"
    )


def verdict_from_sentinel_raw(raw: Any) -> SentinelVerdict:
    if isinstance(raw, SentinelVerdict):
        return SentinelVerdict(
            action=raw.action,
            signals=_safe_signals(list(raw.signals)),
            reasons=rule_ids_only(list(raw.reasons)),
            fail_closed=True,
        )
    decision, risk, signals, findings = _unpack_verdict(raw)
    action = _map_decision(decision, risk)
    reasons = rule_ids_only(findings)
    risk_token = _safe_token(risk)
    if risk_token and f"risk:{risk_token}" not in reasons:
        reasons.append(f"risk:{risk_token}")
    if not reasons:
        reasons = [f"decision:{action.lower()}"]
    return SentinelVerdict(
        action=action,
        signals=_safe_signals(signals),
        reasons=reasons,
        fail_closed=True,
    )


def rule_ids_only(findings: list[Any]) -> list[str]:
    """Map Sentinel findings to rule ids. Never paste artifact text."""
    ids: list[str] = []
    omitted = False
    for item in findings:
        rule_id = _finding_rule_id(item)
        if rule_id:
            if rule_id not in ids:
                ids.append(rule_id)
            continue
        omitted = True
    if omitted and "untrusted artifact text omitted from findings" not in ids:
        ids.append("untrusted artifact text omitted from findings")
    return sanitize_sentinel_reasons(ids)


def sanitize_sentinel_reasons(reasons: list[str]) -> list[str]:
    """Drop attacker/artifact text from findings. URLs are placeholders only."""
    cleaned: list[str] = []
    for reason in reasons:
        text = _URL_RE.sub("<url>", reason.strip())
        lowered = text.lower()
        if (
            text.startswith("{")
            or text.startswith("[")
            or "result_text" in lowered
            or "seed phrase" in lowered
            or "private key" in lowered
            or "ignore previous" in lowered
        ):
            cleaned.append("untrusted artifact text omitted from findings")
            continue
        if len(text) > _MAX_REASON:
            text = text[:_MAX_REASON] + "…"
        if text:
            cleaned.append(text)
    return cleaned or ["sentinel verdict"]


def _require_policy_decide(module: Any) -> Any:
    policy = getattr(module, "policy", None)
    decide = getattr(policy, "decide", None) if policy is not None else None
    if not callable(decide):
        raise AdapterError(
            "flop_sentinel.policy.decide is missing. LocalSentinelAdapter requires "
            "detectors + policy.decide(findings, provenance, affiliation, ...); "
            "top-level decide(artifact_type, artifact)/screen is not the library API."
        )
    return decide


def _require_detectors(module: Any) -> Any:
    detectors = getattr(module, "detectors", None)
    if detectors is None:
        raise AdapterError(
            "flop_sentinel.detectors is missing; LocalSentinelAdapter requires "
            "detectors then policy.decide"
        )
    return detectors


def _invoke_policy_decide(
    decide: Any,
    findings: list[Any],
    provenance: dict[str, Any],
    affiliation: dict[str, Any],
) -> Any:
    try:
        return decide(findings, provenance=provenance, affiliation=affiliation)
    except TypeError:
        try:
            return decide(findings, provenance, affiliation)
        except TypeError:
            try:
                return decide(findings)
            except TypeError as exc:
                raise AdapterError(
                    "flop_sentinel.policy.decide rejected findings/provenance/affiliation"
                ) from exc


def _unpack_verdict(raw: Any) -> tuple[Any, Any, list[Any], list[Any]]:
    if isinstance(raw, dict):
        decision = raw.get("decision") or raw.get("action") or raw.get("verdict")
        risk = raw.get("risk")
        signals = list(raw.get("signals") or [])
        findings = list(raw.get("findings") or raw.get("reasons") or [])
        return decision, risk, signals, findings
    if raw is None:
        raise AdapterError("flop_sentinel.policy.decide returned no verdict; fail closed")
    decision = getattr(raw, "decision", None) or getattr(raw, "action", None)
    risk = getattr(raw, "risk", None)
    signals = list(getattr(raw, "signals", None) or [])
    findings = list(getattr(raw, "findings", None) or getattr(raw, "reasons", None) or [])
    if decision is None and risk is None and not signals and not findings:
        raise AdapterError("flop_sentinel returned an unrecognized verdict shape")
    return decision, risk, signals, findings


def _map_decision(decision: Any, risk: Any) -> Literal["ALLOW", "REJECT", "REVIEW"]:
    token = str(decision or "").strip().upper()
    mapped = _DECISION_MAP.get(token)
    if mapped in {"ALLOW", "REJECT", "REVIEW"}:
        return cast(Literal["ALLOW", "REJECT", "REVIEW"], mapped)
    risk_token = str(risk or "").strip().lower()
    if risk_token in _HIGH_RISK:
        return "REJECT"
    if risk_token in _MEDIUM_RISK:
        return "REVIEW"
    if risk_token in _LOW_RISK and not token:
        return "ALLOW"
    return "REJECT"


def _finding_rule_id(item: Any) -> str | None:
    if isinstance(item, str):
        token = item.strip()
        return token if _RULE_ID_RE.fullmatch(token) else None
    if isinstance(item, dict):
        for key in ("rule_id", "id", "rule", "code"):
            value = item.get(key)
            if isinstance(value, str) and _RULE_ID_RE.fullmatch(value.strip()):
                return value.strip()
        return None
    for attr in ("rule_id", "id", "rule", "code"):
        value = getattr(item, attr, None)
        if isinstance(value, str) and _RULE_ID_RE.fullmatch(value.strip()):
            return value.strip()
    return None


def _safe_signals(signals: list[Any]) -> list[str]:
    out: list[str] = []
    for item in signals:
        token = _safe_token(item)
        if token and token not in out:
            out.append(token)
    return out


def _safe_token(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).strip().replace(" ", "_")
    if not token or not _RULE_ID_RE.fullmatch(token):
        return None
    return token


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _extract_dids(artifact: dict[str, Any]) -> list[str]:
    dids: list[str] = []
    for value in artifact.values():
        if isinstance(value, str) and is_valid_ed25519_did(value) and value not in dids:
            dids.append(value)
    return dids


def _affiliation_from_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
    dids = _extract_dids(artifact)
    family = [did for did in dids if did in KNOWN_FAMILY_DIDS]
    return {
        "operator_group": EXCHANGE_OPERATOR_GROUP,
        "known_family_dids": family,
        "relationship": "same_operator" if family else "unknown",
    }


def _prepare_sys_path(module_path: Path | None) -> None:
    if module_path is None:
        return
    resolved = module_path.expanduser().resolve(strict=False)
    candidates: list[Path] = []
    if resolved.is_file():
        candidates.append(resolved.parent)
        if resolved.parent.name == "flop_sentinel":
            candidates.append(resolved.parent.parent)
    elif resolved.is_dir():
        if (resolved / "__init__.py").exists() and resolved.name == "flop_sentinel":
            candidates.append(resolved.parent)
        candidates.append(resolved)
    for candidate in candidates:
        text = str(candidate)
        if text not in sys.path:
            sys.path.insert(0, text)
