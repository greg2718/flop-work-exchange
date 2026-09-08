from __future__ import annotations

import json
import re
from typing import Any

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


class StubSentinelAdapter:
    """Offline Sentinel-shaped adapter: artifact in, verdict out, fail-closed.

    This is not FLOP Sentinel. Real Sentinel is a pure deterministic library
    (stdlib + cryptography, no network). Swap this stub for a wrapper that
    calls Sentinel when that package is available. Signals match the family
    vocabulary so verdicts compose with Router SECURITY_POLICY.
    """

    def __init__(self, *, seen_templates: set[str] | None = None) -> None:
        self.seen_templates = seen_templates if seen_templates is not None else set()

    def screen(self, artifact_type: str, artifact: dict[str, Any]) -> SentinelVerdict:
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
