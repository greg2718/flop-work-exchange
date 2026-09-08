from __future__ import annotations


class WorkExchangeError(Exception):
    """Base error for FLOP Work Exchange."""


class SafetyError(WorkExchangeError):
    """Unsafe or forbidden action."""


class IsolationError(WorkExchangeError):
    """State or identity overlaps a sibling agent."""


class ValidationError(WorkExchangeError):
    """Invalid input, artifact, or state."""


class StateError(WorkExchangeError):
    """Illegal job lifecycle transition or missing local state."""


class PolicyError(WorkExchangeError):
    """Anti-wash / operator-relationship policy violation."""


class NotLiveError(WorkExchangeError):
    """Live FLOP rails are not available. Settlement execution is DISABLED."""


class AdapterError(WorkExchangeError):
    """Adapter refused or could not complete a request."""


class InsufficientFundsError(WorkExchangeError):
    """Paper ledger account cannot cover a debit."""
