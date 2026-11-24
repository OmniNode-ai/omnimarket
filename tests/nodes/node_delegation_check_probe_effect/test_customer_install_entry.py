# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""C13 grades the customer's own install entry, not the runner's PATH (OMN-26043).

Runner image v15 puts a packaged ``onex`` on the runner's PATH, and a customer install ships its own packaged ``onex`` on PATH
as well. The cloud producer asserted "onex is absent on the runner" and read red on a run whose probe passed. What the check
exists to prove is that the customer path runs through the install's own entry, so the node grades where the customer
machine resolves ``onex`` after the install: a delegate run through any other ``onex`` (the runner's, ahead of the install on
PATH) is a FAIL, and an ``onex`` on the runner's PATH changes nothing for a run through the install's own entry.
"""

from __future__ import annotations

from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_delegation_checks import (
    grade_c13,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    ModelDelegateObservation,
)

BASE = "http://127.0.0.12:8000"
MODEL = "qwen3.8-27b"


def _obs(**over: object) -> ModelDelegateObservation:
    fields: dict[str, object] = {
        "rc": 0,
        "no_checkout": True,
        "three_files_agree": True,
        "receipt_tier": "local",
        "receipt_model": MODEL,
        "receipt_endpoint": BASE,
        "attempt_tiers": ("local",),
        "accepted_last": True,
        "egress_recorded": True,
        "model_connects": 3,
        "token_counter_moved": True,
        "control_external_classified": True,
    }
    return ModelDelegateObservation(**{**fields, **over})


def _failed(obs: ModelDelegateObservation) -> list[str]:
    clauses = grade_c13(obs, responder_base=BASE, served_model=MODEL)
    return [c.name for c in clauses if not c.ok]


def test_a_delegate_run_through_the_runners_onex_fails_install_entry() -> None:
    obs = _obs(entry_path="/usr/local/bin/onex", entry_is_install_own=False)
    assert _failed(obs) == ["install_entry"]


def test_a_run_through_the_installs_own_entry_passes_with_an_onex_on_the_runner() -> (
    None
):
    # The runner carries /usr/local/bin/onex (image v15); the customer machine resolves the install's own entry first.
    obs = _obs(
        entry_path="/srv/customer-install/bin/onex",
        entry_is_install_own=True,
        runner_entry_path="/usr/local/bin/onex",
    )
    assert _failed(obs) == []


def test_an_observation_that_never_read_the_entry_is_not_a_pass() -> None:
    assert _failed(_obs()) == ["install_entry"]


def test_no_clause_asserts_an_onex_absent_from_the_runner() -> None:
    obs = _obs(entry_path="/srv/customer-install/bin/onex", entry_is_install_own=True)
    names = {c.name for c in grade_c13(obs, responder_base=BASE, served_model=MODEL)}
    assert names == {
        "no_checkout",
        "install_entry",
        "three_files",
        "local_model",
        "zero_provider",
    }
