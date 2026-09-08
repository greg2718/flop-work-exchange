from __future__ import annotations

from flop_work_exchange.config import ExchangeConfig, OperatorRelationship, PolicyConfig
from flop_work_exchange.constants import KNOWN_FAMILY_DIDS
from flop_work_exchange.exceptions import PolicyError, SafetyError, ValidationError
from flop_work_exchange.identity import is_valid_ed25519_did

FAUCET_CLAIM_MARKERS = (
    "faucet claim",
    "claimed faucet",
    "faucet drip",
    "room faucet",
    "i claimed",
    "claim the faucet",
    "airdrop code",
    "claim code",
)


def reject_payment_proof_claims(text: str) -> None:
    lowered = text.lower()
    for marker in FAUCET_CLAIM_MARKERS:
        if marker in lowered:
            raise SafetyError(
                "Technocore room faucet/claim messages are not payment proof; "
                "TCLK rail is paper / no value"
            )


def classify_operator_relationship(
    buyer_did: str,
    seller_did: str,
    *,
    config: ExchangeConfig | None = None,
    known_family_dids: frozenset[str] | None = None,
) -> OperatorRelationship:
    family = known_family_dids or (config.known_family_dids if config else KNOWN_FAMILY_DIDS)
    if buyer_did == seller_did:
        return "same_operator"
    buyer_family = buyer_did in family
    seller_family = seller_did in family
    if buyer_family and seller_family:
        return "same_operator"
    if buyer_family or seller_family:
        return "related"
    if config and config.policy.treat_unknown_as_independent:
        return "independent"
    # Distinct DIDs with no recorded common-control evidence remain unknown,
    # except local demo identities which the caller may classify as independent.
    return "unknown"


def relationship_for_demo_identities(buyer_did: str, seller_did: str) -> OperatorRelationship:
    """Local demo keys are distinct and not in the known family."""
    if buyer_did == seller_did:
        return "same_operator"
    if buyer_did in KNOWN_FAMILY_DIDS or seller_did in KNOWN_FAMILY_DIDS:
        return classify_operator_relationship(buyer_did, seller_did)
    if not is_valid_ed25519_did(buyer_did) or not is_valid_ed25519_did(seller_did):
        raise ValidationError("demo identities must be did:key Ed25519 DIDs")
    return "independent"


def apply_deal_policy(
    *,
    buyer_did: str,
    seller_did: str,
    relationship: OperatorRelationship,
    policy: PolicyConfig,
    wash_risk: bool = False,
) -> dict[str, bool]:
    if buyer_did == seller_did and not policy.allow_self_deals:
        raise PolicyError("self-deals are disabled by policy")
    if relationship == "same_operator" and not policy.allow_same_operator_deals:
        raise PolicyError("same_operator deals are disabled by policy")
    independent_rep = (
        relationship == "independent"
        and policy.same_operator_independent_reputation is False
        and not wash_risk
    )
    # Independent reputation is only for independent, non-wash deals.
    if relationship != "independent":
        independent_rep = False
    independent_fees = relationship == "independent" and not wash_risk
    if relationship == "same_operator":
        if policy.same_operator_independent_reputation:
            independent_rep = False
        if policy.same_operator_fee_volume_as_independent:
            independent_fees = False
        independent_rep = False
        independent_fees = False
    return {
        "independent_reputation_eligible": independent_rep,
        "independent_fee_volume_eligible": independent_fees,
        "wash_risk": wash_risk,
    }


def detect_wash_risk(
    *,
    buyer_did: str,
    seller_did: str,
    prior_pairs: list[tuple[str, str]],
) -> bool:
    if buyer_did == seller_did:
        return True
    reversed_pair = (seller_did, buyer_did)
    return reversed_pair in prior_pairs
