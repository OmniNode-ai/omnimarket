# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The CI publishers all read ONE lane transport declaration (OMN-18012).

``config/ci_bus_lanes.yaml`` declares ``security_protocol`` and, for a SASL
protocol, ``sasl_mechanism`` beside each lane's broker. Two publishers resolve
it through two implementations today: ``scripts/ci_bus_lanes.py`` (the shared
module, used by ``publish_pr_merged_event.py`` and
``scripts/trigger_rebuild_on_merge.py``) and the private copy inside
``publish_occ_autobind_command.py``, which is deliberately not consolidated
because the omniclaude reusables pin a three-file sparse-checkout list.

Duplication that nobody compares is how a fail-closed gate drifts into a
green no-op, so these tests compare them: on the REAL checked-in overlay, and on
the malformed shapes, the two must return the same answer. If a future change
moves only one of them, this file goes red.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"
OVERLAY_PATH = REPO_ROOT / "config" / "ci_bus_lanes.yaml"

LIVE_DEV_PROTOCOL = "SASL_PLAINTEXT"
LIVE_DEV_MECHANISM = "SCRAM-SHA-256"


def _load(module_name: str) -> types.ModuleType:
    """Load a scripts/ module the way `python scripts/<name>.py` would."""
    scripts_dir = str(SCRIPTS_DIR)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(
        module_name, SCRIPTS_DIR / f"{module_name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def shared() -> types.ModuleType:
    return _load("ci_bus_lanes")


@pytest.fixture(scope="module")
def occ() -> types.ModuleType:
    return _load("publish_occ_autobind_command")


@pytest.fixture(scope="module")
def live_overlay() -> dict[str, object]:
    loaded = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
def test_checked_in_dev_lane_declares_its_transport(
    live_overlay: dict[str, object],
) -> None:
    """The committed overlay is the source; assert what it actually says."""
    lanes = live_overlay["lanes"]
    assert isinstance(lanes, dict)
    assert lanes["dev"]["security_protocol"] == LIVE_DEV_PROTOCOL
    assert lanes["dev"]["sasl_mechanism"] == LIVE_DEV_MECHANISM


@pytest.mark.unit
def test_both_resolvers_agree_on_the_live_dev_lane(
    shared: types.ModuleType,
    occ: types.ModuleType,
    live_overlay: dict[str, object],
) -> None:
    """The shared module and the OCC publisher's private copy give one answer."""
    from_shared = shared.resolve_lane_security(live_overlay, "dev")
    from_occ = occ._resolve_lane_security(live_overlay, "dev")

    assert from_shared == from_occ == (LIVE_DEV_PROTOCOL, LIVE_DEV_MECHANISM)


@pytest.mark.unit
def test_both_producer_builders_agree_on_the_live_dev_lane(
    shared: types.ModuleType,
    occ: types.ModuleType,
    live_overlay: dict[str, object],
) -> None:
    """Same lane, same credentials, byte-identical librdkafka config."""
    protocol, mechanism = shared.resolve_lane_security(live_overlay, "dev")

    from_shared = shared.build_producer_config(
        "broker:19092", "principal", "secret", protocol, mechanism
    )
    from_occ = occ._kafka_producer_config(
        "broker:19092", "principal", "secret", protocol, mechanism
    )

    assert from_shared == from_occ
    assert from_shared["security.protocol"] == LIVE_DEV_PROTOCOL
    assert from_shared["sasl.mechanisms"] == LIVE_DEV_MECHANISM
    # The regression this ticket exists for: never TLS, never PLAIN, on a lane
    # that declares SASL over an unencrypted listener.
    assert from_shared["security.protocol"] != "SASL_SSL"
    assert from_shared["sasl.mechanisms"] != "PLAIN"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("lane", "entry", "expected"),
    [
        ("undeclared", {}, "declares no transport"),
        ("no_protocol", {"broker": "b:19092"}, "does not declare security_protocol"),
        (
            "bad_protocol",
            {"broker": "b:19092", "security_protocol": "SASL_TLS"},
            "not a librdkafka",
        ),
        (
            "sasl_without_mechanism",
            {"broker": "b:19092", "security_protocol": "SASL_PLAINTEXT"},
            "no sasl_mechanism",
        ),
        (
            "mechanism_without_sasl",
            {
                "broker": "b:19092",
                "security_protocol": "PLAINTEXT",
                "sasl_mechanism": "SCRAM-SHA-256",
            },
            "contradictory",
        ),
    ],
)
def test_both_resolvers_refuse_the_same_malformed_declarations(
    shared: types.ModuleType,
    occ: types.ModuleType,
    lane: str,
    entry: dict[str, str],
    expected: str,
) -> None:
    """Neither implementation may be the lenient one, and both say the same why."""
    overlay: dict[str, object] = {"lanes": {lane: entry} if entry else {}}

    with pytest.raises(ValueError, match=expected):
        shared.resolve_lane_security(overlay, lane)
    with pytest.raises(ValueError, match=expected):
        occ._resolve_lane_security(overlay, lane)


@pytest.mark.unit
def test_sasl_lane_without_credentials_fails_closed_in_both(
    shared: types.ModuleType, occ: types.ModuleType
) -> None:
    """A SASL lane with no principal must red, never downgrade to plaintext."""
    with pytest.raises(ValueError, match="are not set in this job's environment"):
        shared.build_producer_config(
            "b:19092", "", "", LIVE_DEV_PROTOCOL, LIVE_DEV_MECHANISM
        )
    with pytest.raises(ValueError, match="are not set in this job's environment"):
        occ._kafka_producer_config(
            "b:19092", "", "", LIVE_DEV_PROTOCOL, LIVE_DEV_MECHANISM
        )
