from __future__ import annotations

import re

from flop_work_exchange.exceptions import ValidationError

MICRO_PER_FLOP = 1_000_000
INTEGER_RE = re.compile(r"^(0|[1-9][0-9]*)$")
DECIMAL_FLOP_RE = re.compile(r"^(0|[1-9][0-9]*)(\.([0-9]{1,6}))?$")


def parse_micro(value: str | int) -> int:
    """Parse an exact integer micro-unit amount. Floats are rejected."""
    if isinstance(value, bool):
        raise ValidationError("boolean is not a valid amount")
    if isinstance(value, int):
        if value < 0:
            raise ValidationError("amount must be a non-negative integer")
        return value
    text = str(value).strip()
    if not INTEGER_RE.fullmatch(text):
        raise ValidationError(
            "micro-unit amounts must be exact integer strings (no sign, no decimal, no float)"
        )
    return int(text)


def parse_flop_to_micro(value: str | int) -> int:
    """Parse a FLOP amount into integer micro-units without using float.

    Prefer integer micro-units at API boundaries. Decimal FLOP strings are
    accepted only when they have at most 6 fractional digits so they map
    exactly onto integer micro-units. Binary floating point is never used.
    """
    if isinstance(value, bool):
        raise ValidationError("boolean is not a valid FLOP amount")
    if isinstance(value, int):
        if value < 0:
            raise ValidationError("FLOP amount must be non-negative")
        return value * MICRO_PER_FLOP
    text = str(value).strip()
    if INTEGER_RE.fullmatch(text):
        return int(text) * MICRO_PER_FLOP
    match = DECIMAL_FLOP_RE.fullmatch(text)
    if not match:
        raise ValidationError(
            "FLOP amounts must be exact integer or decimal strings with at most 6 fraction digits"
        )
    whole = int(match.group(1))
    frac = match.group(3) or "0"
    frac_micro = int(frac.ljust(6, "0"))
    return whole * MICRO_PER_FLOP + frac_micro


def micro_to_flop_string(amount_micro: int) -> str:
    """Render integer micro-units as a decimal FLOP string without floats."""
    if amount_micro < 0:
        raise ValidationError("amount must be non-negative")
    whole, frac = divmod(amount_micro, MICRO_PER_FLOP)
    if frac == 0:
        return str(whole)
    return f"{whole}.{frac:06d}".rstrip("0")
