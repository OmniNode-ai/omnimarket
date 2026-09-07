"""ONEX envelope unwrapping -- matches omnidash TypeScript parseMessage() exactly."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any, Final

logger = logging.getLogger(__name__)


def unwrap_envelope(raw_bytes: bytes) -> dict[str, Any] | None:
    """Parse a Kafka message value and unwrap ONEX envelope.

    Replicates the omnidash read-model-consumer.ts parseMessage() logic:
    - { payload: { ... } } -> use payload, attach _envelope
    - { data: { ... } } -> use data, attach _envelope, _event_type, _correlation_id
    - Otherwise use raw parsed object
    """
    try:
        raw = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(raw, dict):
        return None

    # Unwrap payload envelope
    if isinstance(raw.get("payload"), dict):
        result = dict(raw["payload"])
        result["_envelope"] = raw
        return result

    # Unwrap data envelope (if data is a dict, not a list)
    data = raw.get("data")
    if isinstance(data, dict):
        result = dict(data)
        result["_envelope"] = raw
        result["_event_type"] = raw.get("event_type")
        result["_correlation_id"] = raw.get("correlation_id")
        return result

    return raw


# The keys ``unwrap_envelope`` above ADDS to the payload it returns. They are a
# transport artifact of this runner, not fields any producer put on the wire.
#
# OMN-16831. Every wire model in the delegation family is declared
# ``extra="forbid"``, so handing one of them the dict this module returns is a
# guaranteed ``ValidationError`` -- ``_envelope`` / ``Extra inputs are not
# permitted``. That is not a hypothetical: it rejected 53 of the 82 records on
# ``onex.dlq.omnimarket.projection-delegation-malformed.v1`` between
# 2026-08-26T12:40:01.508Z and 2026-09-07T11:28:53.769Z, including both source
# events of the two terminal delegations that produced zero
# ``delegation_events`` rows on onex-dev. The offset was committed each time, so
# the loss was silent.
#
# The keys are still injected rather than dropped at the source: the
# delegate-skill terminal path reads ``_envelope`` for its envelope-timestamp
# fallback (``model_delegate_skill_terminal_projection
# ._payload_with_envelope_timestamp``), and the LLM-cost backfill reads
# ``_event_type``/``_correlation_id``. Stripping them in ``unwrap_envelope``
# would break those readers. Stripping them at each typed-model construction is
# the seam that is correct for both kinds of consumer.
RUNNER_INJECTED_KEYS: Final[frozenset[str]] = frozenset(
    {"_envelope", "_event_type", "_correlation_id"}
)


def strip_runner_injected_keys(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``data`` without the keys :func:`unwrap_envelope` added.

    Call this immediately before constructing an ``extra="forbid"`` wire model
    from a runner-delivered payload. It removes exactly the three keys this
    module injects and nothing else -- an unexpected field that a *producer*
    actually put on the wire still fails validation, which is the behaviour
    ``extra="forbid"`` exists for.
    """
    return {
        key: value for key, value in data.items() if key not in RUNNER_INJECTED_KEYS
    }
