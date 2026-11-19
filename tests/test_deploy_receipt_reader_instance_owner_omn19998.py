# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy receipt reader keeps instance, host, lane_mirror, sibling_refs (OMN-19998).

WHAT WAS BROKEN
    The deploy agent's terminal events carry an ownership stamp -- ``instance`` and
    ``host``, set only by an instance with the ``commands_only`` flag -- plus the
    completion's ``lane_mirror``, a per-repository ``sibling_refs`` mapping, and a new
    rejection reason ``route_unconfirmed``. The reader models declared none of them,
    and ``extra="ignore"`` silently discarded every one: a receipt could not say who
    ran the rebuild, whether the departure can be accepted as ours
    (``accepted_as_ours``), or why a command never ran.

WHAT IS PINNED
    Every assertion runs twice -- AFTER PARSING and AFTER FORWARDING, where forwarding
    is ``model_dump(mode="json")``, the form the readers hand on. Two failing controls
    keep the suite honest: the reader rebuilt at its OLD shape (the new fields removed)
    really does discard ``instance``, and an ownership flag written WITHOUT the status
    condition really would accept a FAILED rebuild.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field, create_model

from omnimarket.events.runtime_deployment import (
    EnumDeployRejectionReason,
    EnumRedeployStatus,
    ModelDeployRebuildCompleted,
    ModelDeployRebuildRejected,
)

_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES = _ROOT / "tests" / "fixtures" / "deploy_receipt_omn19998"

# The four keys this step adds to the reader's completion model. The control below
# rebuilds the model with exactly these removed -- its OLD, discarding shape.
_NEW_COMPLETION_FIELDS = ("instance", "host", "lane_mirror", "sibling_refs")


def _load_fixture(name: str) -> dict[str, Any]:
    """Read one receipt fixture from the step's fixture directory."""
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _assert_every_fixture_value_survives(
    fixture: dict[str, Any],
    parsed: BaseModel,
    forwarded: dict[str, Any],
) -> None:
    """Both the parsed model AND the forwarded dump hold every value, equal to the fixture.

    Forwarding is ``model_dump(mode="json")`` -- what the readers hand on -- so a value
    that parses but is dropped on the way out fails here too. Parsed values are typed
    (UUID, enums, nested models); each is reduced to its wire form before the
    comparison so the check is on the VALUE surviving, not on its Python type. A
    timestamp is compared as the INSTANT it names, not its spelling: pydantic dumps
    UTC as ``Z`` where the fixture states ``+00:00``, and both are the same moment.
    """
    for key, expected in fixture.items():
        actual = getattr(parsed, key)
        if isinstance(actual, datetime):
            moment = datetime.fromisoformat(str(expected))
            assert actual == moment, f"parsed model lost or altered {key!r}"
            assert datetime.fromisoformat(str(forwarded[key])) == moment, (
                f"forwarded dump lost or altered {key!r}"
            )
            continue
        if isinstance(actual, UUID):
            actual = str(actual)
        elif isinstance(actual, BaseModel):
            actual = actual.model_dump(mode="json")
        assert actual == expected, f"parsed model lost or altered {key!r}"
        assert forwarded[key] == expected, f"forwarded dump lost or altered {key!r}"


def _completion_model_at_the_old_shape() -> type[BaseModel]:
    """The reader's completion model with the OMN-19998 fields removed.

    This is the shape that shipped as the defect: ``extra="ignore"`` took the four keys
    off the wire without an error. Rebuilt field for field from the CURRENT model minus
    the new fields, so the control cannot drift from the real reader -- a rename
    upstream breaks here loudly instead of pinning a fiction.
    """
    old_fields: dict[str, Any] = {}
    for name, info in ModelDeployRebuildCompleted.model_fields.items():
        if name in _NEW_COMPLETION_FIELDS:
            continue
        if info.default_factory is not None:
            rebuilt = Field(default_factory=info.default_factory)
        elif info.is_required():
            rebuilt = Field(...)
        else:
            rebuilt = Field(default=info.default)
        old_fields[name] = (info.annotation, rebuilt)
    return create_model(
        "DeployRebuildCompletedAtOldShape",
        __config__=ConfigDict(frozen=True, extra="ignore", from_attributes=True),
        **old_fields,
    )


# ---------------------------------------------------------------------------
# The completed receipt: parse + forward.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_owned_completion_receipt_parses_and_forwards_every_value() -> None:
    """An owned success receipt keeps instance/host and accepts the departure as ours."""
    fixture = _load_fixture("completed_owned.json")
    parsed = ModelDeployRebuildCompleted.model_validate(fixture)
    forwarded = parsed.model_dump(mode="json")

    _assert_every_fixture_value_survives(fixture, parsed, forwarded)
    assert parsed.instance == fixture["instance"]
    assert parsed.host == fixture["host"]
    assert parsed.git_sha == fixture["git_sha"]
    assert parsed.lane_mirror is None
    assert parsed.sibling_refs == fixture["sibling_refs"]
    assert parsed.accepted_as_ours is True
    assert forwarded["accepted_as_ours"] is True


@pytest.mark.unit
def test_a_completion_without_instance_and_host_is_not_accepted_as_ours() -> None:
    """The same receipt without the stamp: fields parse to None, flag False."""
    fixture = _load_fixture("completed_not_owned.json")
    parsed = ModelDeployRebuildCompleted.model_validate(fixture)
    forwarded = parsed.model_dump(mode="json")

    assert "instance" not in fixture
    assert "host" not in fixture
    _assert_every_fixture_value_survives(fixture, parsed, forwarded)
    assert parsed.instance is None
    assert parsed.host is None
    assert parsed.accepted_as_ours is False
    assert forwarded["instance"] is None
    assert forwarded["host"] is None
    assert forwarded["accepted_as_ours"] is False


@pytest.mark.unit
def test_an_empty_instance_or_host_is_not_an_ownership_stamp() -> None:
    """Non-empty is the requirement: blank strings never accept the departure."""
    fixture = _load_fixture("completed_owned.json")
    for override in ({"instance": ""}, {"host": ""}, {"instance": "", "host": ""}):
        parsed = ModelDeployRebuildCompleted.model_validate(fixture | override)
        assert parsed.accepted_as_ours is False, f"override {override} was accepted"


@pytest.mark.unit
def test_a_failed_owned_rebuild_is_kept_but_never_accepted() -> None:
    """A failed receipt that names its instance is evidence, not an accepted departure."""
    fixture = _load_fixture("completed_failed_owned.json")
    parsed = ModelDeployRebuildCompleted.model_validate(fixture)
    forwarded = parsed.model_dump(mode="json")

    _assert_every_fixture_value_survives(fixture, parsed, forwarded)
    assert parsed.status is EnumRedeployStatus.FAILED
    assert parsed.errors, "the failed fixture must carry non-empty errors"
    assert parsed.services_restarted == []
    assert parsed.accepted_as_ours is False
    assert forwarded["accepted_as_ours"] is False

    succeeded = ModelDeployRebuildCompleted.model_validate(
        fixture | {"status": "success"}
    )
    assert succeeded.accepted_as_ours is True
    assert succeeded.model_dump(mode="json")["accepted_as_ours"] is True


# ---------------------------------------------------------------------------
# The rejection: parse + forward.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_route_unconfirmed_rejection_parses_and_is_never_accepted() -> None:
    """``route_unconfirmed`` parses, and a rejection is never an accepted departure."""
    fixture = _load_fixture("rejected_route_unconfirmed.json")
    parsed = ModelDeployRebuildRejected.model_validate(fixture)
    forwarded = parsed.model_dump(mode="json")

    _assert_every_fixture_value_survives(fixture, parsed, forwarded)
    assert EnumDeployRejectionReason.ROUTE_UNCONFIRMED.value == "route_unconfirmed"
    assert parsed.reason is EnumDeployRejectionReason.ROUTE_UNCONFIRMED
    assert parsed.instance == fixture["instance"]
    assert parsed.host == fixture["host"]
    assert parsed.accepted_as_ours is False
    assert forwarded["reason"] == "route_unconfirmed"
    assert forwarded["accepted_as_ours"] is False


# ---------------------------------------------------------------------------
# Failing control (i): the OLD reader shape really discards the stamp.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_failing_control_the_old_reader_shape_really_discards_instance() -> None:
    """The suite can catch the discard: at the OLD shape the owned receipt loses instance.

    ``extra="ignore"`` does not raise, it silently drops -- which is why the original
    defect shipped green. The control parses the same owned receipt the first test
    asserts on and shows ``instance``/``host`` gone from model and dump alike, so the
    assertions above fail exactly here if the fields are ever removed again.
    """
    fixture = _load_fixture("completed_owned.json")
    assert "instance" in fixture
    assert "host" in fixture

    control = _completion_model_at_the_old_shape()
    assert "instance" not in control.model_fields
    assert "host" not in control.model_fields

    parsed = control.model_validate(fixture)
    assert not hasattr(parsed, "instance"), (
        "the control model kept instance; it no longer reproduces the old shape"
    )
    assert not hasattr(parsed, "host")
    forwarded = parsed.model_dump(mode="json")
    assert "instance" not in forwarded
    assert "host" not in forwarded
    assert parsed.correlation_id == fixture["correlation_id"], (
        "the control must fail on the DISCARD, not on validation: every other key "
        "parses cleanly under extra=ignore"
    )


# ---------------------------------------------------------------------------
# Failing control (ii): the status condition is load-bearing.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_failing_control_a_flag_without_the_status_gate_accepts_the_failure() -> None:
    """A flag that skips the status condition accepts a FAILED rebuild; the model does not.

    The reviewer's tempting shortcut -- "instance and host present, so ours" -- reads
    True for the failed owned receipt, which would accept a departure that never went
    live. The model reads False, so the status gate is the difference, not the names.
    """

    def claims_ours_without_the_status_gate(
        parsed: ModelDeployRebuildCompleted,
    ) -> bool:
        return bool(parsed.instance) and bool(parsed.host)

    failed = ModelDeployRebuildCompleted.model_validate(
        _load_fixture("completed_failed_owned.json")
    )
    assert claims_ours_without_the_status_gate(failed) is True
    assert failed.accepted_as_ours is False

    owned = ModelDeployRebuildCompleted.model_validate(
        _load_fixture("completed_owned.json")
    )
    assert claims_ours_without_the_status_gate(owned) is True
    assert owned.accepted_as_ours is True
