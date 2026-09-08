from __future__ import annotations

import importlib
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flop_work_exchange.exceptions import AdapterError
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
    """Import flop_sentinel (optional extra / path) and map decide/screen.

    Expected unpublished library contract (any one is accepted):

        flop_sentinel.decide(artifact_type, artifact) -> verdict
        flop_sentinel.screen(artifact_type, artifact) -> verdict
        flop_sentinel.Sentinel().decide(...) / .screen(...)

    Verdict may be a mapping or object with ``action``, ``signals``, and
    ``reasons`` / ``findings``. Attacker/artifact text is never copied into
    findings. Fail closed if the package is missing or the API is unknown.
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
        except AdapterError as exc:
            return {"ok": False, "kind": self.kind, "error": str(exc)}
        return {
            "ok": True,
            "kind": self.kind,
            "module": getattr(module, "__name__", "flop_sentinel"),
            "note": "pure library import; no network; findings redact untrusted text",
        }

    def screen(self, artifact_type: str, artifact: dict[str, Any]) -> SentinelVerdict:
        module = self._load_module()
        raw = _call_sentinel(module, artifact_type, artifact)
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


def verdict_from_sentinel_raw(raw: Any) -> SentinelVerdict:
    if isinstance(raw, SentinelVerdict):
        return SentinelVerdict(
            action=raw.action,
            signals=list(raw.signals),
            reasons=sanitize_sentinel_reasons(list(raw.reasons)),
            fail_closed=True,
        )
    action: Any
    signals: list[str]
    reasons: list[str]
    if isinstance(raw, dict):
        action = raw.get("action") or raw.get("verdict")
        signals = [str(item) for item in (raw.get("signals") or [])]
        reasons = [str(item) for item in (raw.get("reasons") or raw.get("findings") or [])]
    elif hasattr(raw, "action"):
        action = getattr(raw, "action", None)
        signals = [str(item) for item in (getattr(raw, "signals", None) or [])]
        reasons = [
            str(item)
            for item in (
                getattr(raw, "reasons", None) or getattr(raw, "findings", None) or []
            )
        ]
    else:
        raise AdapterError("flop_sentinel returned an unrecognized verdict shape")
    normalized = str(action or "").upper()
    if normalized not in {"ALLOW", "REJECT", "REVIEW"}:
        return SentinelVerdict(
            action="REJECT",
            signals=signals or ["unknown_verdict"],
            reasons=sanitize_sentinel_reasons(
                reasons or [f"unrecognized sentinel action {action!r}; fail closed"]
            ),
        )
    return SentinelVerdict(
        action=normalized,  # type: ignore[arg-type]
        signals=signals,
        reasons=sanitize_sentinel_reasons(reasons or [normalized.lower()]),
    )


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


def _call_sentinel(module: Any, artifact_type: str, artifact: dict[str, Any]) -> Any:
    for name in ("decide", "screen"):
        fn = getattr(module, name, None)
        if callable(fn):
            return fn(artifact_type, artifact)
    sentinel_cls = getattr(module, "Sentinel", None)
    if callable(sentinel_cls):
        instance = sentinel_cls()
        for name in ("decide", "screen"):
            fn = getattr(instance, name, None)
            if callable(fn):
                return fn(artifact_type, artifact)
    raise AdapterError(
        "flop_sentinel has no decide/screen function or Sentinel().decide/screen method"
    )


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
