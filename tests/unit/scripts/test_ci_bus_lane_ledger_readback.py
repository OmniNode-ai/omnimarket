# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane overlay declares a LEDGER DSN by NAME, on the dev lane only.

OMN-16964. ``node_chain_canary_effect``'s link-5 leg (OMN-16025 link 5 --
"complete ledger chain + replay green through an HONEST tier-2 verifier,
SKIP != PASS") had no source declared anywhere, so every chain-canary run
since #3345 has reported ``ledger_replay_not_configured`` at 4 of 5 links
(runs 34355201941, 34356615155). That refusal is correct and the gate is
unclosable until there is a reviewable place to say where the source comes
from.

This is the sibling of the OMN-18060 ``projection_readback`` declaration and
is deliberately a SEPARATE block rather than a reuse of it. The two legs read
two different relations for two different links, and a single shared
declaration would mean neither could ever be repointed without silently moving
the other. That they resolve to the same variable name TODAY is a fact about
this lane's topology -- ``ledger_chain`` and ``delegation_workflow_state`` are
both in the ``omnibase_infra`` database, read by the same least-privilege
identity -- not a coupling to bake into the schema.

These assertions are about the DECLARATION, in the repo the declaration lives
in. The consumer-side refusals live with the consumer (``omnibase_infra``
``node_chain_canary_effect/lane_transport.py``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_PATH = REPO_ROOT / "config" / "ci_bus_lanes.yaml"

#: The one lane the chain canary's ledger readback may be declared on, for the
#: same reason as its projection sibling: the canary is a dev-lane instrument,
#: and a source declared against stability, judge or prod would point a probe
#: at a lane it is not authorized to read.
LEDGER_READBACK_LANE = "dev"

LEDGER_READBACK_KEY = "ledger_readback"
DSN_ENV_KEY = "dsn_env"

ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: Substrings that mean somebody wrote a VALUE where a NAME belongs.
VALUE_MARKERS = ("://", "@", "=", " ")


@pytest.fixture(scope="module")
def lanes() -> dict[str, object]:
    loaded = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{OVERLAY_PATH} is not a YAML mapping"
    declared = loaded.get("lanes")
    assert isinstance(declared, dict), f"{OVERLAY_PATH} declares no lanes mapping"
    return declared


@pytest.mark.unit
def test_dev_lane_declares_a_ledger_readback_dsn_env(lanes: dict[str, object]) -> None:
    """The dev lane names the variable the link-5 source arrives under."""
    dev = lanes.get(LEDGER_READBACK_LANE)
    assert isinstance(dev, dict), "the dev lane is not declared as a mapping"

    block = dev.get(LEDGER_READBACK_KEY)
    assert isinstance(block, dict), (
        f"the dev lane declares no {LEDGER_READBACK_KEY!r} mapping, so "
        "OMN-16025 link 5 has no instrument and the chain canary reports "
        "skipped_not_configured"
    )

    dsn_env = block.get(DSN_ENV_KEY)
    assert isinstance(dsn_env, str), (
        f"{LEDGER_READBACK_KEY}.{DSN_ENV_KEY} is missing or is not a string"
    )
    assert dsn_env.strip(), f"{LEDGER_READBACK_KEY}.{DSN_ENV_KEY} is empty"


@pytest.mark.unit
def test_the_ledger_declaration_is_a_name_and_never_a_value(
    lanes: dict[str, object],
) -> None:
    """A credential pasted here must fail in this PR, not at 01:41Z on a probe."""
    dev = lanes[LEDGER_READBACK_LANE]
    assert isinstance(dev, dict)
    block = dev[LEDGER_READBACK_KEY]
    assert isinstance(block, dict)
    dsn_env = str(block[DSN_ENV_KEY])

    for marker in VALUE_MARKERS:
        assert marker not in dsn_env, (
            f"{LEDGER_READBACK_KEY}.{DSN_ENV_KEY} contains {marker!r}, which "
            "means a VALUE was written where a NAME belongs. This file is "
            "committed, CODEOWNERS-reviewed config and must never carry a "
            "credential."
        )

    assert ENV_NAME_RE.match(dsn_env), (
        f"{dsn_env!r} is not a POSIX environment variable name; the workflow "
        "injects the secret under this literal name, so a name the shell "
        "cannot export is a readback that silently never runs"
    )


@pytest.mark.unit
def test_no_other_lane_declares_a_ledger_readback(lanes: dict[str, object]) -> None:
    """Only the dev lane may declare one. The consumer refuses the rest too."""
    for lane_name, declaration in lanes.items():
        if lane_name == LEDGER_READBACK_LANE:
            continue
        if not isinstance(declaration, dict):
            continue
        assert LEDGER_READBACK_KEY not in declaration, (
            f"lane {lane_name!r} declares a {LEDGER_READBACK_KEY!r} block. The "
            "chain canary is a dev-lane instrument; stability, judge and prod "
            "are read-only surfaces it has no authorization to read."
        )


@pytest.mark.unit
def test_ledger_and_projection_declarations_are_independent_blocks(
    lanes: dict[str, object],
) -> None:
    """Two links, two declarations — never one block serving both.

    They may resolve to the same variable name while both relations live in
    the same database. What must not happen is one block being read for both
    legs, which would make repointing either one silently move the other.
    """
    dev = lanes[LEDGER_READBACK_LANE]
    assert isinstance(dev, dict)
    assert LEDGER_READBACK_KEY in dev
    assert "projection_readback" in dev, (
        "the projection declaration disappeared; link 2 and link 5 are "
        "separate legs and both must stay declared"
    )
