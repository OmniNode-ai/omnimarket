# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18626: every declared lab model id must be one the endpoint answered with.

WHY THIS EXISTS AT ALL.

This repo declares the served model of a lab endpoint in three contracts: the
Bifrost base contract's ``model_name``, the routing tiers' ``id``, and the
task-class overrides keyed to that id. Every one of those is a DECLARATION, and
a declaration's only referent inside this repository is another declaration.
They agree with each other perfectly while all being wrong, which is not a
hypothesis: ``.201:8000`` has now carried three different served ids, and on two
of those occasions every static test in every repo stayed green while every local
delegation was refused and climbed to a metered provider.

    OMN-16419   2026-08-23   the endpoint moved, the contracts did not
    OMN-16999   2026-09-05   the endpoint moved back, the contracts did not
    OMN-18626   2026-09-17   the endpoint moved again, the contracts did not

``tests/fixtures/served_models_probe_201.yaml`` is the one artifact in this repo
with an EXTERNAL referent: a transcript of what the endpoint answered, dated, with
its negative control recorded beside it. This test binds the declarations to it.

WHAT THIS IS NOT, STATED PLAINLY BECAUSE IT MATTERS FOR INCREMENT 2.

This is the CORRECTNESS form of the check: a declared id is permitted to exist as
long as it matches the recorded readback. The reviewer lane working the same
defect one surface over (OMN-18623) made the stronger argument, and it is right:
a gate that permits a declared id as long as it matches something is still a gate
somebody has to keep feeding, and feeding it is the step that gets skipped. The
ABSENCE form -- assert that no concrete model id literal exists for a
single-model local endpoint at all -- cannot be fed wrong because there is nothing
to feed.

The absence form is not available yet. These contracts still carry literals, so a
test demanding their absence would fail by construction. It becomes available in
increment 2, when the served id is resolved at call time from the endpoint and
leaves the contracts entirely, and this file should be DELETED then rather than
carried forward. It is a scaffold with a known demolition date, not a permanent
gate.

NOTHING HERE TOUCHES THE LAB. The fixture is a record and CI never probes: a live
probe in CI would convert an ordinary lab outage into a hard failure in every
consuming repo. Liveness is a runtime concern; identity is a contract concern.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

pytestmark = pytest.mark.unit

_FIXTURE: Final[Path] = (
    Path(__file__).resolve().parents[2] / "fixtures" / "served_models_probe_201.yaml"
)

#: The backends this repo binds to the single-model endpoint the fixture records.
_LOCAL_201_BACKENDS: Final[frozenset[str]] = frozenset(
    {"local-coder", "local-heavy-reasoning"}
)
_LOCAL_201_MODELS_URL: Final[str] = (
    "http://192.168.86.201:8000/v1/models"  # onex-allow-internal-ip OMN-18626 reason="keyed to the recorded lab probe"
)


def _load_probe_fixture() -> dict[str, Any]:
    loaded: Any = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), (
        f"{_FIXTURE.name} did not parse to a mapping; the assertions below have "
        "nothing to read"
    )
    return loaded


def _recorded_served_ids() -> list[str]:
    """The ids the endpoint ANSWERED with, from the dated readback."""
    probes = _load_probe_fixture()["probes"]
    for probe in probes:
        if probe["endpoint"] == _LOCAL_201_MODELS_URL:
            served = list(probe["served_model_ids"])
            assert served, (
                f"{_FIXTURE.name} records an EMPTY served_model_ids for "
                f"{_LOCAL_201_MODELS_URL}. Every assertion below would then pass "
                "vacuously, so this fails closed instead"
            )
            return served
    raise AssertionError(
        f"{_FIXTURE.name} carries no readback for {_LOCAL_201_MODELS_URL}, so "
        "the declarations below have nothing to be checked against. Re-probe the "
        "endpoint and record it rather than deleting this assertion."
    )


def _load_config(resource: str) -> dict[str, Any]:
    path = importlib.resources.files("omnimarket").joinpath(resource)
    loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), (
        f"{resource} did not parse to a mapping, so nothing below can read it"
    )
    return loaded


def _refusal(what: str, declared: str, served: list[str]) -> str:
    return (
        f"{what} declares model id {declared!r}, which "
        f"{_LOCAL_201_MODELS_URL} did not report. It answered {served}.\n\n"
        "vLLM validates the model field and refuses an unknown id BY NAME, so "
        "this is not a mislabel: every local delegation on that rung returns "
        "HTTP 404 and the cheapest-first chain climbs to a metered provider "
        "while the lab GPU sits idle.\n\n"
        "Re-probe the endpoint and update "
        "tests/fixtures/served_models_probe_201.yaml in the SAME commit that "
        "moves the contracts, or the two halves drift again."
    )


def test_bifrost_base_contract_names_an_id_the_endpoint_answered_with() -> None:
    """The wire ``model`` for both .201 rungs must be a served id."""
    served = _recorded_served_ids()
    contract = _load_config("configs/bifrost_delegation.yaml")
    backends = contract["backends"]
    assert isinstance(backends, list)

    checked = 0
    for backend in backends:
        backend_id = backend.get("backend_id")
        if backend_id not in _LOCAL_201_BACKENDS:
            continue
        declared = backend.get("model_name")
        if declared is None:
            # Increment 2's shape: the overlay, and behind it the runtime
            # resolver, own the id. Nothing to check here, by design.
            checked += 1
            continue
        checked += 1
        assert declared in served, _refusal(
            f"bifrost_delegation.yaml backend {backend_id!r}", declared, served
        )

    assert checked == len(_LOCAL_201_BACKENDS), (
        "bifrost_delegation.yaml no longer declares both .201 backends "
        f"({sorted(_LOCAL_201_BACKENDS)}); this test checked {checked} of them "
        "and would pass while leaving a rung unverified"
    )


def test_routing_tier_ids_name_an_id_the_endpoint_answered_with() -> None:
    """The tier ``id`` is the key the task-class overrides resolve through.

    A tier id that names a model the endpoint does not serve does not merely
    mislabel the rung: ``task_class_contracts.v1.yaml``'s id-match escape hatch
    stops resolving, and routing strands.
    """
    served = _recorded_served_ids()
    tiers = _load_config("configs/routing_tiers.yaml")

    checked = 0
    for tier in tiers.get("tiers", []):
        for model in tier.get("models", []):
            if model.get("backend_id") not in _LOCAL_201_BACKENDS:
                continue
            declared = model.get("id")
            checked += 1
            assert declared in served, _refusal(
                f"routing_tiers.yaml tier model for backend "
                f"{model.get('backend_id')!r}",
                declared,
                served,
            )

    assert checked >= len(_LOCAL_201_BACKENDS), (
        "routing_tiers.yaml declares fewer .201 tier rows than there are .201 "
        f"backends; checked {checked}, expected at least "
        f"{len(_LOCAL_201_BACKENDS)}"
    )


def test_the_recorded_readback_is_evidence_and_not_a_restated_literal() -> None:
    """Positive control on the fixture itself.

    The fixture is the only external referent in this repo, so it gets its own
    assertions: a readback with no status, no date or no control is somebody's
    belief written in JSON, which is exactly what the declarations already are.
    """
    probes = _load_probe_fixture()["probes"]
    assert probes, "the fixture records no probes at all"

    for probe in probes:
        endpoint = probe["endpoint"]
        assert probe.get("http_status") == 200, (
            f"{endpoint} has no successful readback recorded; a served_model_ids "
            "list without a 200 beside it is not evidence"
        )
        assert probe.get("probed_at"), f"{endpoint} readback carries no date"
        assert probe.get("command"), (
            f"{endpoint} readback does not record the command that produced it, "
            "so it cannot be reproduced"
        )
        assert probe.get("control"), (
            f"{endpoint} readback carries no control. A 200 from one port is not "
            "evidence that port answered unless something proves a different "
            "port does not."
        )


def test_the_comparison_would_fail_on_a_retired_id() -> None:
    """Negative control: the check is a real check.

    Without this, a refactor that made ``_recorded_served_ids`` return every id
    ever served would leave both tests above green forever while asserting
    nothing -- the precise failure mode this ticket is about, so it gets a
    control rather than a comment.
    """
    served = _recorded_served_ids()
    retired = "Qwen3.6-35B-A3B"
    assert retired not in served, (
        "the recorded readback still lists the retired id, so the assertions "
        "above cannot distinguish a current declaration from a stale one"
    )

    message = _refusal("a synthetic contract", retired, served)
    assert retired in message
    assert served[0] in message, (
        "the refusal must print BOTH the declared and the served id; a message "
        "naming only one leaves the reader unable to see what to change it to"
    )
