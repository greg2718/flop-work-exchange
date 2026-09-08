from __future__ import annotations

import importlib
import inspect
import json
import re
import sys
import time
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Literal, cast, get_args, get_origin

from flop_work_exchange.canonical import canonical_json_bytes, sha256_bytes
from flop_work_exchange.constants import KNOWN_FAMILY_DIDS
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
_MAX_ARTIFACT_BYTES = 1_000_000
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
    "QUARANTINE": "REJECT",
    "QUARANTINED": "REJECT",
    "REVIEW": "REVIEW",
    "WARN": "REVIEW",
    "HOLD": "REVIEW",
    "ESCALATE": "REVIEW",
}
_HIGH_RISK = {"high", "critical", "severe", "reject"}
_MEDIUM_RISK = {"medium", "moderate", "review"}
_LOW_RISK = {"low", "info", "informational", "none", "allow"}
# Confirmed Mac flop_sentinel.models members:
#   Provenance: SIGNED_VERIFIED, SIGNED_INVALID, UNSIGNED, MALFORMED
#   Affiliation: SELF_OPERATED, UNKNOWN
# Paper-FLOP artifacts are unsigned paper jobs, not on-chain signed receipts.
# Do not invent a 'LOCAL' provenance token — that is not in the library enum.
_PAPER_PROVENANCE_NAMES = (
    "UNSIGNED",
    "UNVERIFIED",
    "UNTRUSTED",
    "UNKNOWN",
)
_AFFILIATION_SAME_OPERATOR = (
    "SELF_OPERATED",
    "SAME_OPERATOR",
    "FAMILY",
    "AFFILIATED",
    "RELATED",
)
_AFFILIATION_UNKNOWN = (
    "UNKNOWN",
    "UNAFFILIATED",
    "INDEPENDENT",
    "EXTERNAL",
    "NONE",
)
_MISSING_DECIDE = (
    "flop_sentinel.policy.decide is missing. LocalSentinelAdapter requires "
    "detectors.ALL_DETECTORS + policy.decide(findings, provenance, affiliation, ...); "
    "top-level decide(artifact_type, artifact)/screen is not the library API. "
    "Import flop_sentinel.policy (do not rely on flop_sentinel.policy via getattr)."
)
_MISSING_DETECT_INPUT = (
    "flop_sentinel detectors require detect(message: Message, nt: NormalizedText, now: float). "
    "CLI path: Message(raw=bytes, …), nt=normalize(message.raw.decode()), then "
    "run_all(ALL_DETECTORS, message, nt, now) or detect(message, nt, now). "
    "detect(text: str) is not the library API."
)

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

    Real unpublished library contract (Mac CLI path):

        from flop_sentinel.detectors import ALL_DETECTORS
        from flop_sentinel.detectors.base import run_all
        from flop_sentinel.models import Message, Provenance, Affiliation
        from flop_sentinel.normalize import normalize
        from flop_sentinel.policy import decide

        message = Message(
            raw=payload_bytes, claimed_did=..., signature=None, sender_id=...,
            room=..., nonce=...,
        )
        nt = normalize(message.raw.decode("utf-8"))
        findings, detector_error = run_all(ALL_DETECTORS, message, nt, now)
        verdict = decide(
            findings, Provenance.UNSIGNED, Affiliation.UNKNOWN,  # or SELF_OPERATED
            detector_error=detector_error, oversized=...,
            detector_versions={d.DETECTOR_ID: d.VERSION for d in ALL_DETECTORS},
            artifact_sha256=...,
        )

    ``ALL_DETECTORS`` are already-instantiated objects. Prefer ``run_all``;
    otherwise call ``detect(message, nt, now)`` — never ``detect(text)``.

    ``policy`` / ``detectors`` / ``models`` are submodules. They are loaded with
    ``importlib.import_module`` — ``getattr(flop_sentinel, "policy")`` is not
    enough when ``__init__.py`` does not re-export them.

    Findings copied into Work Exchange contain **rule ids only** — never
    artifact/attacker text. Fail closed if the package or API is missing.
    Top-level ``decide(artifact_type, artifact)`` / ``screen`` are not the API.
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
            _require_all_detectors(module)
            _require_models(module)
            _require_detector_contract(module)
        except AdapterError as exc:
            return {"ok": False, "kind": self.kind, "error": str(exc)}
        return {
            "ok": True,
            "kind": self.kind,
            "module": getattr(module, "__name__", "flop_sentinel"),
            "api": (
                "flop_sentinel.detectors.base.run_all(ALL_DETECTORS, Message, "
                "NormalizedText, now) → policy.decide(findings, Provenance.UNSIGNED, "
                "Affiliation, *; detector_error, oversized, detector_versions, sha256)"
            ),
            "note": "typed policy.decide; findings are rule ids only; no network",
        }

    def screen(self, artifact_type: str, artifact: dict[str, Any]) -> SentinelVerdict:
        module = self._load_module()
        decide = _require_policy_decide(module)
        models = _require_models(module)
        blob = canonical_json_bytes(artifact)
        oversized = len(blob) > _MAX_ARTIFACT_BYTES
        findings, detector_error, versions = collect_sentinel_findings(
            module, artifact_type, artifact
        )
        provenance = _typed_arg(
            decide,
            models,
            param="provenance",
            model_name="Provenance",
            names=_PAPER_PROVENANCE_NAMES,
        )
        affiliation = _typed_arg(
            decide,
            models,
            param="affiliation",
            model_name="Affiliation",
            names=_affiliation_names(artifact),
        )
        raw = _invoke_policy_decide(
            decide,
            findings,
            provenance,
            affiliation,
            detector_error=detector_error,
            oversized=oversized,
            detector_versions=versions,
            artifact_sha256=sha256_bytes(blob),
        )
        return verdict_from_sentinel_raw(raw)

    def _load_module(self) -> Any:
        if self._module is not None:
            return self._module
        if self._importer is not None:
            self._module = self._importer()
            return self._module
        _prepare_sys_path(self.module_path)
        if self.module_path is not None:
            _forget_loaded_sentinel()
        try:
            self._module = importlib.import_module("flop_sentinel")
        except ImportError as exc:
            raise AdapterError(
                "LocalSentinelAdapter: flop_sentinel is not importable. Install a local "
                "checkout (`pip install -e ~/dev/flop_sentinel`) or set "
                f"FLOP_WX_SENTINEL_PATH. Import error: {exc}"
            ) from exc
        return self._module


def collect_sentinel_findings(
    module: Any, artifact_type: str, artifact: dict[str, Any]
) -> tuple[list[Any], bool, dict[str, str]]:
    """Run ALL_DETECTORS via the CLI pipeline. Returns (findings, detector_error, versions)."""
    collection = tuple(
        _instantiate_detector(item)[0] for item in _require_all_detectors(module)
    )
    message, nt, now = _prepare_detector_input(module, artifact_type, artifact)
    versions = {
        _detector_id(detector): _detector_version(detector) for detector in collection
    }
    run_all = _resolve_run_all(module)
    if run_all is not None:
        try:
            produced = run_all(collection, message, nt, now)
        except TypeError:
            pass
        except Exception:
            return [], True, versions
        else:
            findings, detector_error = _unpack_run_all(produced)
            return (
                [item for item in findings if _finding_is_positive(item)],
                detector_error,
                versions,
            )
    findings, detector_error = _run_each_detector(collection, message, nt, now)
    return findings, detector_error, versions


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
    policy = _submodule(module, "policy")
    decide = getattr(policy, "decide", None) if policy is not None else None
    if decide is not None and not callable(decide):
        nested = getattr(decide, "decide", None)
        decide = nested if callable(nested) else None
    if not callable(decide):
        raise AdapterError(_MISSING_DECIDE)
    return decide


def _require_all_detectors(module: Any) -> tuple[Any, ...]:
    detectors = _submodule(module, "detectors")
    collection = getattr(detectors, "ALL_DETECTORS", None) if detectors is not None else None
    if collection is None:
        raise AdapterError(
            "flop_sentinel.detectors.ALL_DETECTORS is missing. LocalSentinelAdapter "
            "requires a list/tuple of Detector instances "
            "(detect(message, nt, now)); run/detect/DETECTORS/REGISTRY "
            "is not the library API."
        )
    if not isinstance(collection, (list, tuple)):
        raise AdapterError(
            "flop_sentinel.detectors.ALL_DETECTORS must be a list or tuple of Detector instances"
        )
    return tuple(collection)


def _require_detector_contract(module: Any) -> None:
    """CLI types: Message + normalize(); detect(text) is not the API."""
    _resolve_message_cls(module)
    _require_normalize(module)


def _resolve_message_cls(module: Any) -> Any:
    models = _submodule(module, "models")
    cls = getattr(models, "Message", None) if models is not None else None
    if cls is not None:
        return cls
    raise AdapterError(
        "flop_sentinel.models.Message is missing. CLI constructs "
        "Message(raw=bytes, claimed_did, signature, sender_id, room, nonce)."
    )


def _require_normalize(module: Any) -> Any:
    normalize_mod = _submodule(module, "normalize")
    fn = getattr(normalize_mod, "normalize", None) if normalize_mod is not None else None
    if not callable(fn) or inspect.isclass(fn):
        raise AdapterError(
            "flop_sentinel.normalize.normalize is missing. CLI path is "
            "nt = normalize(message.raw.decode(...)); do not invent a parallel normalizer."
        )
    return fn


def _require_models(module: Any) -> Any:
    models = _submodule(module, "models")
    if models is None:
        raise AdapterError(
            "flop_sentinel.models is missing. LocalSentinelAdapter requires typed "
            "Provenance, Affiliation, Finding, and Verdict — not dicts."
        )
    missing = [
        name
        for name in ("Provenance", "Affiliation", "Verdict")
        if getattr(models, name, None) is None
    ]
    if missing:
        raise AdapterError(
            "flop_sentinel.models is missing "
            + ", ".join(missing)
            + "; policy.decide uses typed enums/dataclasses"
        )
    return models


def _submodule(module: Any, name: str) -> Any | None:
    """Load ``flop_sentinel.<name>``.

    ``getattr(package, "policy")`` does **not** import a submodule unless
    ``__init__.py`` re-exports it. Injectable test doubles attach non-module
    objects; those win. Otherwise load the submodule via importlib.
    """
    attached = getattr(module, name, None)
    if attached is not None and not inspect.ismodule(attached):
        return attached
    pkg = getattr(module, "__name__", None)
    if isinstance(pkg, str) and pkg:
        try:
            return importlib.import_module(f"{pkg}.{name}")
        except ImportError:
            pass
    return attached


def _invoke_policy_decide(
    decide: Any,
    findings: list[Any],
    provenance: Any,
    affiliation: Any,
    *,
    detector_error: bool,
    oversized: bool,
    detector_versions: Mapping[str, str],
    artifact_sha256: str,
) -> Any:
    extras = {
        "detector_error": detector_error,
        "oversized": oversized,
        "detector_versions": dict(detector_versions),
        "artifact_sha256": artifact_sha256,
    }
    attempts: tuple[tuple[tuple[Any, ...], dict[str, Any]], ...] = (
        ((tuple(findings), provenance, affiliation), extras),
        ((findings, provenance, affiliation), extras),
        ((tuple(findings),), {"provenance": provenance, "affiliation": affiliation, **extras}),
        ((findings,), {"provenance": provenance, "affiliation": affiliation, **extras}),
        ((tuple(findings), provenance, affiliation), {}),
        ((findings, provenance, affiliation), {}),
    )
    last_exc: TypeError | None = None
    for args, kwargs in attempts:
        try:
            return _call_compatible(decide, args, kwargs)
        except TypeError as exc:
            last_exc = exc
    raise AdapterError(
        "flop_sentinel.policy.decide rejected typed findings/provenance/affiliation"
    ) from last_exc


def _call_compatible(fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    accepted: dict[str, Any] = {}
    var_kw = False
    named = dict(kwargs)
    leftover_args = list(args)
    for param in signature.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            var_kw = True
            continue
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        if param.name in named:
            accepted[param.name] = named.pop(param.name)
            continue
        if leftover_args and param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            accepted[param.name] = leftover_args.pop(0)
    if var_kw:
        accepted.update(named)
    return fn(**accepted) if accepted or not args else fn(*args, **kwargs)


def _typed_arg(
    decide: Any,
    models: Any,
    *,
    param: str,
    model_name: str,
    names: tuple[str, ...],
) -> Any:
    cls = getattr(models, model_name, None)
    annotation = _param_annotation(decide, param)
    resolved = _resolve_annotation(annotation, models)
    if resolved is not None:
        cls = resolved
    if cls is None:
        raise AdapterError(
            f"flop_sentinel.models.{model_name} is required; passing a dict is not the API"
        )
    member = _enum_member(cls, *names)
    if member is not None:
        return member
    available = _enum_labels(cls)
    hint = f" (available: {', '.join(available)})" if available else ""
    raise AdapterError(
        f"cannot map {names[0]!r} onto flop_sentinel.models.{model_name}{hint}; fail closed"
    )


def _param_annotation(fn: Any, name: str) -> Any:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return inspect.Parameter.empty
    param = signature.parameters.get(name)
    if param is None:
        return inspect.Parameter.empty
    return param.annotation


def _resolve_annotation(annotation: Any, models: Any) -> Any | None:
    if annotation is inspect.Parameter.empty:
        return None
    origin = get_origin(annotation)
    if origin is not None:
        args = [item for item in get_args(annotation) if item is not type(None)]
        return _resolve_annotation(args[0], models) if args else None
    if isinstance(annotation, str):
        return getattr(models, annotation, None)
    return annotation


def _enum_member(cls: Any, *names: str) -> Any | None:
    if not _is_enum_type(cls):
        return None
    members = list(cls)
    by_name = {str(member.name).upper(): member for member in members}
    by_value = {str(getattr(member, "value", "")).upper(): member for member in members}
    for name in names:
        key = name.upper()
        if key in by_name:
            return by_name[key]
        if key in by_value:
            return by_value[key]
    return None


def _enum_labels(cls: Any) -> list[str]:
    if not _is_enum_type(cls):
        return []
    return [str(member.name) for member in cls]


def _is_enum_type(cls: Any) -> bool:
    return isinstance(cls, type) and issubclass(cls, Enum)


def _affiliation_names(artifact: dict[str, Any]) -> tuple[str, ...]:
    explicit = artifact.get("operator_relationship")
    if isinstance(explicit, str) and explicit.strip():
        token = explicit.strip().upper()
        if token in {"SAME_OPERATOR", "RELATED", "SELF_OPERATED"}:
            return ("SELF_OPERATED", token, *_AFFILIATION_SAME_OPERATOR, *_AFFILIATION_UNKNOWN)
        if token in {"INDEPENDENT", "UNKNOWN"}:
            return (token, *_AFFILIATION_UNKNOWN)
    dids = _extract_dids(artifact)
    if any(did in KNOWN_FAMILY_DIDS for did in dids):
        return (*_AFFILIATION_SAME_OPERATOR, *_AFFILIATION_UNKNOWN)
    return (*_AFFILIATION_UNKNOWN, *_AFFILIATION_SAME_OPERATOR)


def _instantiate_detector(item: Any) -> tuple[Any, str, str]:
    detector: Any
    if inspect.isclass(item):
        try:
            detector = item()
        except TypeError:
            detector = item
    else:
        detector = item
    return detector, _detector_id(detector), _detector_version(detector)


def _detector_id(detector: Any) -> str:
    value = (
        getattr(detector, "DETECTOR_ID", None)
        or getattr(detector, "name", None)
        or getattr(type(detector), "__name__", None)
        or "detector"
    )
    return str(value)


def _detector_version(detector: Any) -> str:
    value = getattr(detector, "VERSION", None) or getattr(detector, "version", None) or "unknown"
    return str(value)


def _resolve_run_all(module: Any) -> Any | None:
    detectors = _submodule(module, "detectors")
    attached_base = getattr(detectors, "base", None) if detectors is not None else None
    for src in (attached_base, detectors):
        if src is not None and not inspect.ismodule(src):
            fn = getattr(src, "run_all", None)
            if callable(fn):
                return fn
    pkg = getattr(module, "__name__", None)
    if isinstance(pkg, str) and pkg:
        try:
            base = importlib.import_module(f"{pkg}.detectors.base")
        except ImportError:
            base = None
        if base is not None:
            fn = getattr(base, "run_all", None)
            if callable(fn):
                return fn
    if attached_base is not None:
        fn = getattr(attached_base, "run_all", None)
        if callable(fn):
            return fn
    return None


def _unpack_run_all(produced: Any) -> tuple[list[Any], bool]:
    if isinstance(produced, tuple) and len(produced) == 2:
        findings, detector_error = produced
        return _as_list(findings), bool(detector_error)
    raise AdapterError(
        "flop_sentinel.detectors.base.run_all must return (findings, detector_error)"
    )


def _run_each_detector(
    collection: tuple[Any, ...], message: Any, nt: Any, now: float
) -> tuple[list[Any], bool]:
    findings: list[Any] = []
    detector_error = False
    for detector in collection:
        try:
            produced = _run_detector(detector, message=message, nt=nt, now=now)
        except AdapterError:
            detector_error = True
            continue
        except Exception:
            detector_error = True
            continue
        findings.extend(item for item in _as_list(produced) if _finding_is_positive(item))
    return findings, detector_error


def _run_detector(
    detector: Any,
    *,
    message: Any,
    nt: Any,
    now: float,
) -> Any:
    fn = getattr(detector, "detect", None)
    if not callable(fn):
        raise AdapterError("detector has no detect(message, nt, now) method")
    try:
        return fn(message, nt, now)
    except TypeError as exc:
        raise AdapterError(_MISSING_DETECT_INPUT) from exc


def _prepare_detector_input(
    module: Any, artifact_type: str, artifact: dict[str, Any]
) -> tuple[Any, Any, float]:
    message = _build_cli_message(module, artifact_type, artifact)
    raw = getattr(message, "raw", None)
    if not isinstance(raw, (bytes, bytearray)):
        raise AdapterError("flop_sentinel.models.Message.raw must be bytes")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdapterError("Message.raw is not utf-8; fail closed") from exc
    nt = _require_normalize(module)(text)
    return message, nt, time.time()


def _build_cli_message(module: Any, artifact_type: str, artifact: dict[str, Any]) -> Any:
    cls = _resolve_message_cls(module)
    did = _sender_from_artifact(artifact)
    claimed = artifact.get("claimed_did")
    if not isinstance(claimed, str) or not claimed.strip():
        claimed = did
    sender = artifact.get("sender_id")
    if not isinstance(sender, str) or not sender.strip():
        sender_did = artifact.get("sender_did")
        sender = sender_did if isinstance(sender_did, str) and sender_did.strip() else did
    nonce = artifact.get("nonce")
    if not isinstance(nonce, str) or not nonce.strip():
        job_id = artifact.get("job_id")
        nonce = job_id if isinstance(job_id, str) else None
    kwargs = {
        "raw": canonical_json_bytes(artifact),
        "claimed_did": claimed,
        "signature": None,
        "sender_id": sender,
        "room": artifact_type,
        "nonce": nonce,
    }
    try:
        return _call_compatible(cls, (), kwargs)
    except TypeError as exc:
        raise AdapterError(
            "cannot construct flop_sentinel.models.Message(raw=bytes, claimed_did, "
            "signature, sender_id, room, nonce); fail closed"
        ) from exc


def _sender_from_artifact(artifact: dict[str, Any]) -> str | None:
    for key in ("sender_did", "buyer_did", "seller_did", "claimed_did", "sender_id", "sender"):
        value = artifact.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _finding_is_positive(item: Any) -> bool:
    """Drop explicit negative/false findings; empty returns stay empty."""
    keys = ("matched", "fired", "positive", "hit", "true")
    if isinstance(item, dict):
        flags = [item.get(key) for key in keys]
    else:
        flags = [getattr(item, key, None) for key in keys]
    explicit = [flag for flag in flags if flag is not None]
    if not explicit:
        return True
    return any(bool(flag) for flag in explicit)


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
    token = _enum_or_str(decision).strip().upper()
    if "." in token and token.split(".")[-1] in _DECISION_MAP:
        token = token.split(".")[-1]
    mapped = _DECISION_MAP.get(token)
    if mapped in {"ALLOW", "REJECT", "REVIEW"}:
        return cast(Literal["ALLOW", "REJECT", "REVIEW"], mapped)
    risk_token = _enum_or_str(risk).strip().lower()
    if "." in risk_token:
        risk_token = risk_token.split(".")[-1]
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
            token = _enum_or_str(value).strip()
            if token and _RULE_ID_RE.fullmatch(token):
                return token
        return None
    for attr in ("rule_id", "id", "rule", "code"):
        value = getattr(item, attr, None)
        token = _enum_or_str(value).strip()
        if token and _RULE_ID_RE.fullmatch(token):
            return token
    return None


def _safe_signals(signals: list[Any]) -> list[str]:
    out: list[str] = []
    for item in signals:
        token = _safe_token(item)
        if token and token not in out:
            out.append(token)
    return out


def _safe_token(value: Any) -> str | None:
    token = _enum_or_str(value).strip().replace(" ", "_")
    if not token or not _RULE_ID_RE.fullmatch(token):
        return None
    return token


def _enum_or_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Enum):
        raw = value.value if isinstance(value.value, str) else value.name
        return str(raw)
    return str(value)


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


def _forget_loaded_sentinel() -> None:
    for name in list(sys.modules):
        if name == "flop_sentinel" or name.startswith("flop_sentinel."):
            del sys.modules[name]


def _prepare_sys_path(module_path: Path | None) -> None:
    if module_path is None:
        return
    resolved = module_path.expanduser().resolve(strict=False)
    if resolved.is_file():
        resolved = resolved.parent
    if not resolved.is_dir():
        return
    priority: list[Path] = []
    src_pkg = resolved / "src" / "flop_sentinel"
    if src_pkg.is_dir():
        priority.append(resolved / "src")
    nested = resolved / "flop_sentinel"
    if (nested / "__init__.py").exists() or (nested / "policy.py").exists():
        priority.append(resolved)
    if resolved.name == "flop_sentinel" and resolved.parent.name == "src":
        priority.append(resolved.parent)
    if resolved.name == "flop_sentinel" and (
        (resolved / "__init__.py").exists() or (resolved / "policy.py").exists()
    ):
        priority.append(resolved.parent)
    priority.append(resolved)
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in priority:
        text = str(candidate)
        if text not in seen:
            seen.add(text)
            ordered.append(text)
    for text in reversed(ordered):
        if text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)
    importlib.invalidate_caches()
