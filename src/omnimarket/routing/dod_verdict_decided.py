"""Which delegation DoD verdict rows judged the attempt (OMN-22690)."""

from __future__ import annotations

#: Verdict statuses that decide an attempt (``verified`` or ``failed``).
DOD_DECIDED_STATUSES: frozenset[str] = frozenset({"verified", "failed"})

#: The verifier's code for "no DoD contract was found for the ticket"
#: (``node_dod_verify`` evidence_collector and handler_dod_verify). A verdict
#: that failed on it checked nothing about the delegated run.
DOD_CONTRACT_MISSING_CODE = "CONTRACT_MISSING"


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def is_decided(row: dict[str, object]) -> bool:
    """True when the verdict judged the attempt: verified or failed, not on a missing contract."""
    if _as_text(row.get("verdict_status")) not in DOD_DECIDED_STATUSES:
        return False
    return DOD_CONTRACT_MISSING_CODE not in _as_text(row.get("verdict_error_message"))
