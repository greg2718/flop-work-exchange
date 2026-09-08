from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from flop_work_exchange.canonical import atomic_write_text, canonical_json_bytes, load_json_object
from flop_work_exchange.constants import (
    DEFAULT_PRODUCTION_STATE,
    EXCHANGE_OPERATOR_GROUP,
    KNOWN_FAMILY_AGENTS,
    assert_isolated_state_dir,
    assert_not_production_auto_init,
)
from flop_work_exchange.exceptions import SafetyError, ValidationError

B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ED25519_MULTICODEC = b"\xed\x01"
DID_RE = re.compile(r"^did:key:z[1-9A-HJ-NP-Za-km-z]+$")
IDENTITY_CONFIRMATION = "CREATE-FLOP-WORK-EXCHANGE-IDENTITY"
TEST_IDENTITY_PEM = "identity-test-only.pem"
TEST_IDENTITY_JSON = "identity-test-only.json"
PRODUCTION_IDENTITY_PEM = "identity.pem"
PRODUCTION_IDENTITY_JSON = "identity.json"


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = bytearray()
    while n:
        n, r = divmod(n, 58)
        out.append(B58[r])
    zeros = len(raw) - len(raw.lstrip(b"\0"))
    encoded = bytes(reversed(out)) if out else b""
    return (B58[:1] * zeros + encoded).decode("ascii")


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text.encode("ascii"):
        try:
            value = B58.index(ch)
        except ValueError as exc:
            raise ValueError("Invalid base58 character") from exc
        n = n * 58 + value
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    zeros = len(text) - len(text.lstrip("1"))
    return b"\0" * zeros + raw


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64u_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def public_did_from_raw(public_key: bytes) -> str:
    return "did:key:z" + b58encode(ED25519_MULTICODEC + public_key)


def public_did(key: Ed25519PrivateKey) -> str:
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return public_did_from_raw(pub)


def is_valid_ed25519_did(did: str) -> bool:
    if not DID_RE.fullmatch(did):
        return False
    try:
        decoded = b58decode(did.removeprefix("did:key:z"))
    except ValueError:
        return False
    return decoded.startswith(ED25519_MULTICODEC) and len(decoded) == 34


def require_did(did: str) -> str:
    if not is_valid_ed25519_did(did):
        raise ValidationError(f"invalid Ed25519 did:key: {did}")
    return did


def public_key_from_did(did: str) -> Ed25519PublicKey:
    require_did(did)
    decoded = b58decode(did.removeprefix("did:key:z"))
    return Ed25519PublicKey.from_public_bytes(decoded[len(ED25519_MULTICODEC) :])


def generate_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _write_pem(
    path: Path, key: Ed25519PrivateKey, *, encrypted: bool, passphrase: str | None
) -> None:
    if encrypted:
        if not passphrase:
            raise SafetyError("encrypted identity requires a passphrase")
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(passphrase.encode("utf-8")),
        )
    else:
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    atomic_write_text(path, pem.decode("ascii"), mode=0o600)


def _metadata(
    did: str,
    *,
    purpose: str,
    persistent: bool,
    private_key_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": "flop-work-exchange.identity.v0.1",
        "created_at": datetime.now(UTC).isoformat(),
        "did": did,
        "key_type": "Ed25519",
        "purpose": purpose,
        "persistent": persistent,
        "private_key_path": str(private_key_path),
        "operator_group": {
            "common_control_disclosure": True,
            "operator_group_id": EXCHANGE_OPERATOR_GROUP,
            "related_agents": [agent["name"] for agent in KNOWN_FAMILY_AGENTS],
            "note": (
                "Scout, Bench, Router, Sentinel, and Work Exchange are related agents "
                "under common operator control. They must not count as independent peers."
            ),
        },
    }


def create_test_identity(
    state_dir: Path, *, key: Ed25519PrivateKey | None = None
) -> dict[str, Any]:
    resolved = assert_isolated_state_dir(state_dir)
    assert_not_production_auto_init(resolved)
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    pem_path = resolved / TEST_IDENTITY_PEM
    json_path = resolved / TEST_IDENTITY_JSON
    if pem_path.exists() or json_path.exists():
        raise SafetyError("test identity already exists; refusing to overwrite")
    private_key = key or generate_key()
    did = public_did(private_key)
    _write_pem(pem_path, private_key, encrypted=False, passphrase=None)
    meta = _metadata(did, purpose="test-only", persistent=False, private_key_path=pem_path)
    atomic_write_text(json_path, json.dumps(meta, indent=2, sort_keys=True) + "\n", mode=0o600)
    return meta


def load_private_key(pem_path: Path, passphrase: str | None = None) -> Ed25519PrivateKey:
    pem_bytes = pem_path.read_bytes()
    password = passphrase.encode("utf-8") if passphrase else None
    loaded = serialization.load_pem_private_key(pem_bytes, password=password)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise ValidationError("identity PEM does not contain an Ed25519 private key")
    return loaded


def load_identity_meta(state_dir: Path) -> dict[str, Any]:
    resolved = assert_isolated_state_dir(state_dir)
    for name in (TEST_IDENTITY_JSON, PRODUCTION_IDENTITY_JSON):
        path = resolved / name
        if path.exists():
            return load_json_object(path)
    raise ValidationError("no exchange identity found; run identity init or demo")


def load_exchange_key(
    state_dir: Path, passphrase: str | None = None
) -> tuple[Ed25519PrivateKey, str]:
    resolved = assert_isolated_state_dir(state_dir)
    test_pem = resolved / TEST_IDENTITY_PEM
    prod_pem = resolved / PRODUCTION_IDENTITY_PEM
    if test_pem.exists():
        key = load_private_key(test_pem)
        meta = load_json_object(resolved / TEST_IDENTITY_JSON)
        return key, str(meta["did"])
    if prod_pem.exists():
        key = load_private_key(prod_pem, passphrase=passphrase)
        meta = load_json_object(resolved / PRODUCTION_IDENTITY_JSON)
        return key, str(meta["did"])
    raise ValidationError("no exchange identity found; run identity init or demo")


def ensure_test_identity(state_dir: Path) -> dict[str, Any]:
    resolved = assert_isolated_state_dir(state_dir)
    json_path = resolved / TEST_IDENTITY_JSON
    if json_path.exists():
        return load_json_object(json_path)
    return create_test_identity(resolved)


def create_ephemeral_party(label: str) -> tuple[Ed25519PrivateKey, str]:
    key = generate_key()
    return key, public_did(key)


def create_production_identity(
    *,
    state_dir: Path,
    confirm: str,
    passphrase: str,
    passphrase_confirmation: str,
) -> dict[str, Any]:
    if confirm != IDENTITY_CONFIRMATION:
        raise SafetyError("explicit identity creation confirmation value is required")
    resolved = assert_isolated_state_dir(state_dir)
    expected = DEFAULT_PRODUCTION_STATE.expanduser().resolve(strict=False)
    if resolved != expected:
        raise SafetyError(
            "production identity state directory must resolve exactly to Work Exchange state"
        )
    if passphrase != passphrase_confirmation:
        raise SafetyError("passphrase confirmation does not match")
    if len(passphrase) < 16:
        raise SafetyError("passphrase must be at least 16 characters")
    resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
    pem_path = resolved / PRODUCTION_IDENTITY_PEM
    json_path = resolved / PRODUCTION_IDENTITY_JSON
    if pem_path.exists() or json_path.exists():
        raise SafetyError("production identity already exists; refusing to overwrite")
    key = generate_key()
    did = public_did(key)
    _write_pem(pem_path, key, encrypted=True, passphrase=passphrase)
    meta = _metadata(
        did,
        purpose="flop-work-exchange-production",
        persistent=True,
        private_key_path=pem_path,
    )
    atomic_write_text(json_path, json.dumps(meta, indent=2, sort_keys=True) + "\n", mode=0o600)
    return meta


def sign_bytes(key: Ed25519PrivateKey, payload: bytes) -> str:
    return b64u_encode(key.sign(payload))


def sign_canonical(key: Ed25519PrivateKey, value: Any) -> str:
    return sign_bytes(key, canonical_json_bytes(value))


def verify_bytes(did: str, payload: bytes, signature_b64u: str) -> None:
    public_key = public_key_from_did(did)
    try:
        public_key.verify(b64u_decode(signature_b64u), payload)
    except Exception as exc:
        raise ValidationError("Ed25519 signature verification failed") from exc


def verify_canonical(did: str, value: Any, signature_b64u: str) -> None:
    verify_bytes(did, canonical_json_bytes(value), signature_b64u)
