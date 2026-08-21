# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures for node_event_emit_effect unit tests.

OMN-16167: ``HandlerEventEmitEffect._build_default_adapter()`` no longer
treats an absent ``KAFKA_BOOTSTRAP_SERVERS`` as an implicit "run spool-only"
signal -- that now requires the explicit, contract-declared
``ONEX_EMIT_EFFECT_SPOOL_ONLY`` opt-out (see the node's ``contract.yaml``
``env_dependencies`` block). This unit-test package IS exactly the
legitimate "intentionally no Kafka target configured yet" case the opt-out
exists for (per the ticket's own framing), so it is declared once here for
every test in this package instead of repeated per test. A test that
specifically exercises the new fail-loud path (e.g.
``test_kafka_unconfigured_without_opt_out_fails_loudly``) deletes the
opt-out itself via ``monkeypatch.delenv`` to prove the underlying default
holds without it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_spool_only_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default this whole unit-test package to the declared spool-only lane."""
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", "true")
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)


__all__: list[str] = []
