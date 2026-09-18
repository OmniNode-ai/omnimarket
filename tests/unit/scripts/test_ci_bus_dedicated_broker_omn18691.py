# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18691: the CI bus must never be declared at a RUNTIME LANE's broker.

WHAT THIS REFUSES, AND WHY IT IS A TEST AND NOT A COMMENT.

Until 2026-09-18 the fleet's CI bus WAS a runtime lane's broker: the `dev` lane
entry in this same overlay, `omninode-pc…:19092`, which is the Redpanda of
compose project `omnibase-infra`. That lane's own map entry calls it a "fully
mutable test platform" and the deploy agent rebuilds it on every
runtime-affecting merge. Every rebuild was therefore a window in which the whole
fleet's CI could not publish, and on 2026-09-18 a watchdog kill mid-recreate
left that broker REMOVED: every Receipt Gate in the org was red for ~35 minutes,
on pull requests that had nothing to do with the dev lane.

The operator's ruling was a dedicated broker on its own compose project
(in-session 2026-09-18T14:21:54Z, "dedicated broker is fine",
docs/tracking/ROLLING_WORK_LEDGER.md:4277), and explicitly NOT the stability-test
lane's Redpanda -- that lane is where the `stability-proven` promotion premise is
resolved from on the compose path, so fleet CI traffic on it would turn every CI
publish into a write to a promotion-evidence surface.

A dedicated broker is one line of YAML away from being un-dedicated again. The
regression has no symptom until the next lane rebuild, and by then the person who
edited the line is gone. So the property is asserted here, on the file, rather
than left to review.

WHERE THE AUTHORITATIVE PORT-COLLISION CHECK LIVES, AND WHY NOT HERE.
The set of ports each runtime lane actually binds is in `omnibase_infra`
(`deploy/lane-census/lane-manifest.yaml` and the lane compose files), and that
repo's `tests/unit/docker/test_ci_bus_broker_isolation_omn18691.py` derives the
collision check from those files LIVE, so a lane that claims a new port later
moves the assertion with it. That check cannot run here: this repo does not have
those files, and fetching them would recreate the cross-repo coupling that
produced three red-CI incidents already (OMN-18127).

What THIS module asserts is the half that is resolvable from this file alone and
is the half that actually regressed: the CI-bus lane's declared endpoint must not
be any other lane's declared endpoint, and must not be a known runtime-lane
endpoint even when that lane is declared `inmemory` here.

THAT SECOND CLAUSE IS THE SUBTLE ONE. `stability` and `prod` are `inmemory` in
this overlay, which does NOT mean they have no broker -- it means no CI publisher
may target them. They have real brokers on real ports, and a CI bus declared at
one of those ports would collide on the host while passing any check that only
compares declared strings. The port set below is that knowledge, duplicated from
omnibase_infra deliberately and with its residual stated in ``_LANE_HOST_PORTS``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_PATH = REPO_ROOT / "config" / "ci_bus_lanes.yaml"

#: The lane key the CI publishers will select once the cutover happens.
CI_BUS_LANE = "ci-bus"

#: Host ports bound by a RUNTIME lane's Redpanda on .201, read off the host
#: read-only on 2026-09-18 (`ss -ltn`) and cross-checked against
#: omnibase_infra `deploy/lane-census/lane-manifest.yaml` and the lane compose
#: files:
#:
#:     19092  dev                 (compose project omnibase-infra)
#:     39092  stability-test      (omnibase-infra-stability-test)
#:     49092  judge               (omnibase-infra-judge)
#:     55092  lakshman            (omnibase-infra-lakshman)
#:     59092  a further lane-class listener observed on the host
#:
#: DUPLICATION, ACKNOWLEDGED. These numbers live authoritatively in
#: omnibase_infra. They are restated here because the alternative -- a new
#: top-level key in this overlay -- would break the cross-repo consumer, whose
#: `ModelCiBusOverlay` is `extra="forbid"` and lives in omnibase_infra. Adding a
#: key here without a lockstep change there is precisely the OMN-18012 /
#: OMN-18060 / OMN-18127 failure: the key lands green on this repo's PR and
#: reddens somebody else's unrelated infra PR afterwards.
#:
#: RESIDUAL, STATED RATHER THAN IMPLIED. This list going stale is safe in one
#: direction only. A lane ADDED here that the CI bus does not use costs nothing;
#: a lane that MOVES to a new port not listed here could be claimed by a future
#: CI-bus edit without this test noticing. That direction is covered by the
#: omnibase_infra test named in the module docstring, which reads the live files.
#: Neither test replaces the other.
_LANE_HOST_PORTS: frozenset[str] = frozenset(
    {"19092", "39092", "49092", "55092", "59092"}
)


def _load_overlay(path: Path = OVERLAY_PATH) -> dict[str, Any]:
    """Read the committed overlay, refusing an absent or malformed file.

    Never a skip. A skip here reports green on the change that deleted the file
    this module exists to constrain.
    """
    if not path.is_file():
        raise AssertionError(
            f"{path} is missing. This test compared nothing; it did not pass."
        )
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"{path} did not parse as a mapping.")
    return loaded


def _lanes(overlay: dict[str, Any]) -> dict[str, Any]:
    lanes = overlay.get("lanes")
    assert isinstance(lanes, dict), "the overlay's `lanes` key is not a mapping"
    assert lanes, "the overlay declares no lanes"
    return lanes


def _broker_of(lane_entry: Any) -> str:
    """The lane's declared broker string, or '' when it declares none."""
    raw = lane_entry.get("broker") if isinstance(lane_entry, dict) else lane_entry
    return "" if raw is None else str(raw).strip()


def _port_of(broker: str) -> str:
    """The port half of a `host:port` broker, or '' for a non-concrete value."""
    if ":" not in broker:
        return ""
    return broker.rsplit(":", 1)[1].strip()


@pytest.fixture(scope="module")
def overlay() -> dict[str, Any]:
    return _load_overlay()


def assert_ci_bus_endpoint_is_dedicated(overlay: dict[str, Any]) -> None:
    """Refuse a CI-bus endpoint that is, or collides with, a runtime lane's.

    Exposed as a plain function so the negative cases below drive the SAME code
    path the positive case does. A guard whose failure branch is only ever
    exercised by a hand-written fixture is a guard nobody has proven fires.
    """
    lanes = _lanes(overlay)
    assert CI_BUS_LANE in lanes, (
        f"the overlay must declare a {CI_BUS_LANE!r} lane. The fleet's CI bus is "
        "a dedicated broker (OMN-18691); without this entry a CI publisher can "
        "only be pointed at a runtime lane, which is the defect."
    )

    ci_broker = _broker_of(lanes[CI_BUS_LANE])
    _concrete_broker_required = (
        f"the {CI_BUS_LANE!r} lane must declare a CONCRETE host:port broker, got "
        f"{ci_broker!r}. `inmemory` would make every CI publish a silent no-op "
        "skip and `from-secret` would restore the opaque-secret drift OMN-14800 "
        "was filed for."
    )
    assert ci_broker, _concrete_broker_required
    assert ci_broker not in ("inmemory", "from-secret"), _concrete_broker_required

    for lane_name, entry in lanes.items():
        if lane_name == CI_BUS_LANE:
            continue
        other = _broker_of(entry)
        assert other != ci_broker, (
            f"the CI bus is declared at {ci_broker!r}, which is also lane "
            f"{lane_name!r}'s broker. A CI bus sharing a RUNTIME lane's broker "
            "is destroyed by that lane's next rebuild -- the 2026-09-18 outage "
            "that took every Receipt Gate in the org red for ~35 minutes. "
            "Operator ruling: the CI bus is a DEDICATED broker on its own "
            "compose project."
        )

    ci_port = _port_of(ci_broker)
    assert ci_port not in _LANE_HOST_PORTS, (
        f"the CI bus is declared on port {ci_port!r}, which a runtime lane's "
        "Redpanda binds on .201. Note this catches the case a string comparison "
        "cannot: a lane declared `inmemory` HERE still has a real broker THERE, "
        "and two projects cannot bind one host port."
    )


def test_ci_bus_lane_is_declared_and_dedicated(overlay: dict[str, Any]) -> None:
    """The committed overlay satisfies the guard."""
    assert_ci_bus_endpoint_is_dedicated(overlay)


def test_ci_bus_lane_declares_its_transport(overlay: dict[str, Any]) -> None:
    """Transport is DECLARED, never inferred from the presence of credentials.

    This is the OMN-18012 lesson applied to the new lane rather than rediscovered
    on it: a publisher that picked SASL_SSL because credentials happened to be in
    its environment died on an SSL handshake against a listener that speaks none,
    93 times in one run.
    """
    entry = _lanes(overlay)[CI_BUS_LANE]
    assert entry.get("security_protocol") == "SASL_PLAINTEXT", (
        "the CI-bus listener is SASL over PLAINTEXT -- authenticated, not "
        "encrypted. Declare it; the publisher must not guess."
    )
    assert entry.get("sasl_mechanism") == "SCRAM-SHA-256", (
        "a SASL_* protocol requires a declared mechanism, or the publisher "
        "fail-fasts rather than guessing."
    )


def test_ci_bus_entry_carries_no_credential(overlay: dict[str, Any]) -> None:
    """Nothing in this file is a credential and nothing in it ever becomes one."""
    entry = _lanes(overlay)[CI_BUS_LANE]
    forbidden = {"sasl_username", "sasl_password", "username", "password", "secret"}
    present = forbidden & set(entry)
    assert not present, (
        f"the {CI_BUS_LANE!r} entry carries {sorted(present)}. Broker addresses "
        "are config; credentials live in secrets.KAFKA_SASL_{USERNAME,PASSWORD} "
        "and are never committed here."
    )


def _with_ci_bus_broker(overlay: dict[str, Any], broker: str) -> dict[str, Any]:
    """The real overlay with exactly one value changed.

    Mutating the committed document rather than hand-building a fixture is what
    makes the negative cases exercise the guard on the shape it will actually
    meet in a diff.
    """
    return {
        **overlay,
        "lanes": {
            **_lanes(overlay),
            CI_BUS_LANE: {**_lanes(overlay)[CI_BUS_LANE], "broker": broker},
        },
    }


def test_guard_refuses_another_lanes_declared_endpoint(
    overlay: dict[str, Any],
) -> None:
    """POSITIVE CONTROL for the string clause, on every other declared lane.

    The candidate endpoints are DERIVED from the overlay rather than written
    out. That keeps real lab addresses out of this file, and it means a lane
    whose broker moves is still covered without anybody editing a fixture.
    """
    others = {
        name: _broker_of(entry)
        for name, entry in _lanes(overlay).items()
        if name != CI_BUS_LANE
    }
    concrete = {
        name: broker
        for name, broker in others.items()
        if broker and broker not in ("inmemory", "from-secret")
    }
    assert concrete, (
        "no other lane declares a concrete broker, so this control proved "
        "nothing. It did not pass."
    )
    for broker in concrete.values():
        with pytest.raises(AssertionError):
            assert_ci_bus_endpoint_is_dedicated(_with_ci_bus_broker(overlay, broker))


def test_guard_refuses_a_runtime_lane_port_on_any_host(
    overlay: dict[str, Any],
) -> None:
    """POSITIVE CONTROL for the port clause, including the `inmemory` case.

    The host is a placeholder on purpose: the clause under test is about the
    PORT, and a lane declared `inmemory` in this overlay still binds a real one
    on the lab host. Constructing the candidate proves that without naming a
    real address.
    """
    for port in sorted(_LANE_HOST_PORTS):
        candidate = f"lab-host.invalid:{port}"
        with pytest.raises(AssertionError):
            assert_ci_bus_endpoint_is_dedicated(_with_ci_bus_broker(overlay, candidate))


@pytest.mark.parametrize(
    ("broker", "because"),
    [
        ("inmemory", "a silent no-op skip for every CI publish"),
        ("from-secret", "the opaque-secret drift OMN-14800 was filed for"),
        ("", "an undeclared broker"),
    ],
)
def test_guard_refuses_a_non_concrete_broker(
    overlay: dict[str, Any], broker: str, because: str
) -> None:
    """POSITIVE CONTROL for the concreteness clause."""
    with pytest.raises(AssertionError):
        assert_ci_bus_endpoint_is_dedicated(_with_ci_bus_broker(overlay, broker))


def test_guard_refuses_a_missing_ci_bus_lane(overlay: dict[str, Any]) -> None:
    """Deleting the lane must fail loudly, not degrade to 'no CI bus declared'."""
    without = {
        **overlay,
        "lanes": {k: v for k, v in _lanes(overlay).items() if k != CI_BUS_LANE},
    }
    with pytest.raises(AssertionError):
        assert_ci_bus_endpoint_is_dedicated(without)


def test_no_publisher_selects_the_ci_bus_lane_yet(overlay: dict[str, Any]) -> None:
    """Phase 1 DECLARES the lane; it does not cut over to it.

    The cutover is ordered -- a consumer attaches to the dedicated broker BEFORE
    any publisher is repointed -- because a publisher repointed first would be
    green and silent: the command lands durably on a topic no consumer group
    reads, the workflow reports success, and the companion is never minted. That
    is strictly worse than today's red publish and is the OMN-17378 class.

    This test is the interlock. It fails the moment a workflow selects
    `--lane ci-bus`, which is the right moment for a human to confirm the
    consumer is attached, and it is DELETED (not weakened) by the phase-3 change
    that performs the cutover.
    """
    assert CI_BUS_LANE in _lanes(overlay)
    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no workflows found; this test compared nothing"
    selecting = [
        wf.name
        for wf in workflows
        if f"--lane {CI_BUS_LANE}" in wf.read_text(encoding="utf-8")
        or f"lane: {CI_BUS_LANE}" in wf.read_text(encoding="utf-8")
    ]
    assert not selecting, (
        f"{selecting} already select the {CI_BUS_LANE!r} lane, but phase 1 only "
        "DECLARES it. Repointing a publisher before a consumer is attached to "
        "the dedicated broker makes the publish green and silent. If the "
        "consumer IS attached, delete this test in the same change that cuts "
        "over -- do not weaken it."
    )
