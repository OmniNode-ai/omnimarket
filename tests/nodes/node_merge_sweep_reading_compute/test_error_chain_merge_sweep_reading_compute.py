# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: a reading request the node cannot evaluate as asked is refused by name (OMN-21069).

A time that is not UTC, an out-of-scope repository with no ruling, a ``checks_state`` outside its
four forms and a request that names no fleet are each refused with the reason, through the request
types the packaged contract routes to.
"""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

NAME = "node_merge_sweep_reading_compute"
NOW = "2026-10-09T12:10:00Z"


def _request_types() -> dict[str, Any]:
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    out = {}
    for entry in contract["handler_routing"]["handlers"]:
        module, _, name = str(entry["input_model"]).rpartition(".")
        out[entry["operation"]] = getattr(importlib.import_module(module), name)
    return out


TYPES = _request_types()
READ = TYPES["read_merge_sweep_state"]
CLAIM = TYPES["check_merge_sweep_claim"]


def _pr(**extra: Any) -> dict[str, Any]:
    return {
        "repo": "omnimarket",
        "number": 7,
        "base": "dev",
        "head_ref": "b",
        **extra,
    }


def _read(**extra: Any) -> Any:
    return READ.model_validate({"now": NOW, "fleet": ["omnimarket"], **extra})


def _refused(match: str, build: Any) -> None:
    with pytest.raises(ValidationError, match=match):
        build()


def test_a_well_formed_request_is_accepted() -> None:
    """Positive control: the shapes refused below differ from this one in one field each."""
    request = _read(
        out_of_scope={"omnidash": "RULING 2026-10-10T17:28:07Z"},
        open_prs=[_pr(checks_state="skipped:draft")],
    )
    assert request.out_of_scope == {"omnidash": "RULING 2026-10-10T17:28:07Z"}
    assert CLAIM.model_validate({"pr": "omnimarket#7", "now": NOW}).now == NOW


@pytest.mark.parametrize(
    "now",
    ["2026-10-09T12:10:00", "2026-10-09T08:10:00-04:00", "2026-10-09T13:10:00+01:00"],
)
def test_a_now_that_is_not_utc_is_refused(now: str) -> None:
    _refused("is not UTC", lambda: _read(now=now))
    _refused("is not UTC", lambda: CLAIM.model_validate({"pr": "a#1", "now": now}))


def test_a_now_that_is_not_a_time_is_refused() -> None:
    _refused("is not an ISO time", lambda: _read(now="yesterday"))


@pytest.mark.parametrize("ruling", ["", "   "])
def test_an_out_of_scope_repository_with_no_ruling_is_refused(ruling: str) -> None:
    _refused("names no ruling", lambda: _read(out_of_scope={"omnidash": ruling}))


@pytest.mark.parametrize(
    "state", ["green", "skipped:", "unread: ", "rollup", "READ", "skipped"]
)
def test_a_checks_state_outside_its_four_forms_is_refused(state: str) -> None:
    _refused(
        "is not one of read, rollup-only, skipped:<why>, unread:<why>",
        lambda: _read(open_prs=[_pr(checks_state=state, runs=[])]),
    )


def test_checks_read_with_no_runs_is_refused() -> None:
    _refused("carries no runs", lambda: _read(open_prs=[_pr(checks_state="read")]))


def test_a_request_that_names_no_fleet_is_refused() -> None:
    _refused("fleet", lambda: READ.model_validate({"now": NOW}))
    _refused("fleet", lambda: _read(fleet=[]))
    _refused("empty repository", lambda: _read(fleet=["omnimarket", " "]))
