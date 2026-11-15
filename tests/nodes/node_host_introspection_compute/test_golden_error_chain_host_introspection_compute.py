# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of the host introspection compute (OMN-19998).

Golden: readings in, one sorted announcement out, carrying the capacity advertisement nested and a digest of the stable
facts. Error chain: a host with no table row, a row that says unavailable, a capacity refusal, an ambiguous reading and an
announcement whose digest does not match its facts. The failing control is the digest rule: a producer that stops
covering the runtimes is refused by the model's own check, which is what a registry-side reader relies on.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models import (
    model_host_introspection_announcement as announcement_module,
)
from omnimarket.models.model_host_introspection_announcement import (
    EnumHostAnnouncementReason,
    ModelHostIntrospectionAnnouncement,
    ModelHostRuntime,
)
from omnimarket.models.model_host_introspection_request import ModelHostRowFacts
from omnimarket.nodes.node_host_introspection_compute import (
    HandlerHostIntrospection,
    HostIntrospectionRefusedError,
)
from omnimarket.nodes.node_host_introspection_compute.handlers import (
    handler_host_introspection,
)
from tests.host_introspection_support import (
    capacity,
    facts,
    request,
)

pytestmark = pytest.mark.unit


def _handle(**kwargs: object) -> ModelHostIntrospectionAnnouncement:
    return HandlerHostIntrospection().handle(request(**kwargs))  # type: ignore[arg-type]


def test_golden_the_announcement_is_sorted_and_carries_the_capacity() -> None:
    a = _handle()
    assert a.host_name == "host-a.test"
    assert a.reason is EnumHostAnnouncementReason.START
    assert a.roles == ("lab", "lab-proof")
    assert [g.name for g in a.gpus] == ["gpu-a", "gpu-b"]
    assert [r.name for r in a.runtimes] == ["python", "uv"]
    assert [(s.kind.value, s.name) for s in a.software] == [
        ("clone", "omnimarket"),
        ("package", "omnimarket"),
    ]
    assert a.capacity == capacity()
    assert a.availability.available is True
    assert a.availability.reason == ""
    assert a.facts_digest == a.expected_facts_digest()


def test_golden_an_account_can_run_lanes_exactly_when_its_tool_is_advertised() -> None:
    accounts = {x.account_name: x.can_run_lanes for x in _handle().lane_accounts}
    assert accounts == {"claude": True, "codex": False}


def test_golden_the_same_request_gives_the_same_announcement() -> None:
    assert _handle() == _handle()


def test_the_digest_ignores_what_moves_on_every_beat() -> None:
    quiet = _handle()
    busy = _handle(
        cap=capacity(load1=19.0, mem_available_bytes=1024),
        host_facts=facts(gpu_used=5 * 1024**3),
    )
    assert busy.facts_digest == quiet.facts_digest


def test_the_digest_moves_when_an_installed_version_or_commit_moves() -> None:
    base = _handle().facts_digest
    assert _handle(host_facts=facts(python_version="3.13.0")).facts_digest != base
    assert _handle(host_facts=facts(commit="b" * 40)).facts_digest != base


def test_error_a_host_with_no_table_row_still_announces_and_says_it_takes_no_work() -> (
    None
):
    a = _handle(row=ModelHostRowFacts(declared=False))
    assert a.roles == ()
    assert a.availability.available is False
    assert "no row in the lab host table" in a.availability.reason


def test_error_a_row_marked_unavailable_gives_its_own_reason() -> None:
    a = _handle(
        row=ModelHostRowFacts(
            declared=True,
            roles=("lab",),
            available=False,
            unavailable_reason="in repair",
        )
    )
    assert (a.availability.available, a.availability.reason) == (False, "in repair")


def test_error_a_capacity_refusal_makes_the_host_unavailable_with_its_text() -> None:
    a = _handle(cap=capacity(refusal="swap activity over the bar"))
    assert a.availability.available is False
    assert a.availability.reason == "swap activity over the bar"


def test_error_two_rows_for_one_runtime_are_refused_not_merged() -> None:
    twice = facts().model_copy(
        update={
            "runtimes": (
                ModelHostRuntime(name="python", version="3.12"),
                ModelHostRuntime(name="python", version="3.13"),
            )
        }
    )
    with pytest.raises(
        HostIntrospectionRefusedError, match="runtime named more than once: python"
    ):
        _handle(host_facts=twice)


def test_error_a_capacity_advertisement_of_another_host_is_refused() -> None:
    with pytest.raises(
        HostIntrospectionRefusedError, match="capacity advertisement is for"
    ):
        _handle(cap=capacity(host="host-b.test"))


def test_control_a_digest_that_ignores_the_runtimes_is_refused_by_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injected break: the producer's digest drops the runtimes. The model's own check names the mismatch."""
    real = announcement_module.facts_digest

    def drops_runtimes(**kwargs: object) -> str:
        kwargs["runtimes"] = ()
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(handler_host_introspection, "facts_digest", drops_runtimes)
    with pytest.raises(
        ValidationError, match="does not match the announcement's facts"
    ):
        _handle()
    # without the break the same request is accepted: the red is the break's
    monkeypatch.undo()
    assert _handle().facts_digest
