# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Turn a declared response contract into a provider-native constraint (OMN-18989).

A caller that declares a ``response_contract`` has already told the platform
the exact shape it will accept, including a closed label set expressed as a
JSON-Schema ``enum``. Until now that declaration reached the model only as
appended system-prompt prose, and was enforced only after the fact by the
quality gate.

**Enforcing it after the fact is not the same as constraining it.** Measured on
the live lab endpoint 2026-09-21, same prompt and same minute: asked in prose
to answer with one of ``HEALTH_SIGNAL`` or ``INSTRUMENTATION_DEFECT``, the
served ``Qwen3.8-27B`` returned ``INSTRUMENTEMENT_DEFECT`` — a corruption of
the second label — three times out of three, and the run terminalised
``completed`` at quality 1.0 against a 0.8 bar. The same request carrying
``response_format: {"type": "json_schema", ...}`` over the same two labels
returned valid members only. Correlations
``a75a211c-0828-4714-ad7e-610f226529fe`` and
``582e957c-d2bf-49c8-83e7-5e545d0e7765``.

**Why this is gated on a declared capability rather than always sent.** Both
the request model and the contract-rendering module refuse to send this
directive today, for two distinct and correct reasons. Sent to a provider that
silently IGNORES it, the caller believes it constrained the response when it
did not, which is the silent-fidelity class. Sent to one that REJECTS it, a
gradeable near-miss becomes an HTTP 400, which is strictly worse than the
status quo. The rendering module named the missing piece exactly — "a
capability field on the binding contract" — and this module is the other half
of that sentence.

So the rule is: send it only where the binding declares support, and declare
support only from a live probe of that backend.

**What this does NOT do.** It constrains the SET a label is drawn from. It
says nothing about whether the label chosen is the right one. In the probe
above, the enum-constrained answer picked a different classification from the
prose answer and neither carried any signal about which was correct. A
membership constraint makes an out-of-vocabulary label impossible; a
wrong-but-in-vocabulary label is invisible to it and always will be.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)

#: The ``name`` field OpenAI-compatible providers require beside the schema.
#: A constant rather than something derived from the contract: the name is a
#: provider-side label with no semantics here, and deriving it from caller
#: content would make two callers with equivalent schemas send different
#: bytes for no reason.
STRUCTURED_OUTPUT_SCHEMA_NAME: str = "onex_response_contract"


def provider_response_format_for_contract(
    *,
    backend: ModelDelegationBackendConfig,
    response_contract: dict[str, object] | None,
) -> dict[str, Any] | None:
    """The provider-native ``response_format``, or ``None`` to send nothing.

    Returns ``None`` — meaning the outbound payload carries no
    ``response_format`` key at all, exactly as today — in either of the two
    cases where sending one would be wrong:

    * the backend does not declare structured-output support, so the directive
      would either be ignored (silent fidelity loss) or rejected (HTTP 400);
    * the caller declared no contract, so there is no constraint to express
      and inventing one would guess at what the caller wanted.

    The caller's declared contract is forwarded **verbatim**. Rewriting or
    tightening it would constrain something the caller did not ask for, and
    would put the shape the provider enforces out of step with the shape the
    quality gate grades against — two constraints that disagree are worse than
    one.
    """
    if not backend.supports_response_format_json_schema:
        return None
    if response_contract is None:
        return None
    return {
        "type": "json_schema",
        "json_schema": {
            "name": STRUCTURED_OUTPUT_SCHEMA_NAME,
            "schema": response_contract,
        },
    }


#: The packaged binding contract, resolved from this module's own location so
#: no absolute path is hardcoded and the value is correct in a worktree.
PACKAGED_BINDING_CONTRACT: Path = (
    Path(__file__).resolve().parent.parent / "configs" / "bifrost_delegation.yaml"
)


def load_backends_declaring_structured_output(
    config_path: Path | None = None,
) -> frozenset[str]:
    """Backend ids whose binding declares structured-output support.

    Reads the binding config rather than a hardcoded list, so a backend added
    or a declaration withdrawn is reflected without a code change. Used by the
    drift test that keeps a measured declaration from quietly becoming an
    unmeasured one.

    ``config_path`` defaults to the PACKAGED contract and is passed
    explicitly, because the loader refuses to resolve a default itself (rule 8
    — no silent config fallback, OMN-15628). Naming the packaged file here is
    not that fallback: this function answers "what does the shipped contract
    declare", which is a question about the packaged file specifically. A
    caller asking about a DEPLOYED overlay passes its path.
    """
    from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
        load_bifrost_delegation_config,
    )

    config = load_bifrost_delegation_config(
        config_path=config_path or PACKAGED_BINDING_CONTRACT
    )
    return frozenset(
        backend.backend_id
        for backend in config.backends
        if backend.supports_response_format_json_schema
    )


__all__: list[str] = [
    "PACKAGED_BINDING_CONTRACT",
    "STRUCTURED_OUTPUT_SCHEMA_NAME",
    "load_backends_declaring_structured_output",
    "provider_response_format_for_contract",
]
