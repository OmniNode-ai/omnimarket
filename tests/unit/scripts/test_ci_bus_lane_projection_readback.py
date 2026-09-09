# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane overlay declares a projection DSN by NAME, on the dev lane only.

OMN-18060. ``node_chain_canary_effect``'s link-2 readback (OMN-16025 link 2 --
"routing decision PUBLISHED and PROJECTED, readback from projection, not
logs") had no DSN declared anywhere, so chain-canary run 34281968883 failed
closed on ``projection_readback_not_configured``. That refusal was correct and
the gate was unclosable: there was no reviewable place to say where the DSN
comes from.

``config/ci_bus_lanes.yaml`` is that place, for the same reason it already
carries the broker and the transport -- one checked-in, CODEOWNERS-reviewable
declaration that every CI client of this lane reads, rather than an opaque
secret whose meaning nobody can diff (the OMN-14800 silent-repoint class).

These assertions are about the DECLARATION, in the repo the declaration lives
in. The consumer-side refusals live with the consumer
(``omnibase_infra`` ``node_chain_canary_effect/lane_transport.py``); this file
exists so that a value pasted where a name belongs, or a projection DSN
declared against a lane the canary may not read, is caught in the PR that adds
it rather than by a probe at 01:41Z.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_PATH = REPO_ROOT / "config" / "ci_bus_lanes.yaml"

#: The one lane the chain canary's projection readback may be declared on.
#: `.github/workflows/chain-canary.yml` in omnibase_infra states the scope in
#: its own header: "Dev lane ONLY (`omnibase-infra`, the pre-authorized
#: fully-mutable test platform)". A DSN declared against stability, judge or
#: prod would point a probe at a lane it is not authorized to read.
PROJECTION_READBACK_LANE = "dev"

PROJECTION_READBACK_KEY = "projection_readback"
DSN_ENV_KEY = "dsn_env"

#: A POSIX-shell environment variable NAME. Deliberately narrow: an entry that
#: is a NAME cannot also be a DSN, because a DSN needs at least a ``:`` and a
#: ``/``, and neither is in this class.
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: Substrings that mean somebody wrote a VALUE where a NAME belongs. `://` is
#: the scheme separator of every libpq URI form; `@` separates userinfo from
#: host; `=` is the keyword/value form (`host=... password=...`).
VALUE_MARKERS = ("://", "@", "=", " ")


@pytest.fixture(scope="module")
def lanes() -> dict[str, object]:
    loaded = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{OVERLAY_PATH} is not a YAML mapping"
    declared = loaded.get("lanes")
    assert isinstance(declared, dict), f"{OVERLAY_PATH} declares no lanes mapping"
    return declared


@pytest.mark.unit
def test_dev_lane_declares_a_projection_dsn_env_name(lanes: dict[str, object]) -> None:
    """The dev lane names the variable the canary's DSN arrives under.

    Without this the readback reports SKIPPED_NOT_CONFIGURED forever and link 2
    can never be proven -- which is precisely the state run 34281968883
    recorded.
    """
    entry = lanes.get(PROJECTION_READBACK_LANE)
    assert isinstance(entry, dict), (
        f"lane {PROJECTION_READBACK_LANE!r} must be a mapping in {OVERLAY_PATH}"
    )
    block = entry.get(PROJECTION_READBACK_KEY)
    assert isinstance(block, dict), (
        f"lane {PROJECTION_READBACK_LANE!r} must declare a "
        f"{PROJECTION_READBACK_KEY!r} mapping; without it node_chain_canary_"
        "effect reports SKIPPED_NOT_CONFIGURED and OMN-16025 link 2 is "
        "permanently unproven"
    )
    name = block.get(DSN_ENV_KEY)
    assert isinstance(name, str), (
        f"{PROJECTION_READBACK_KEY}.{DSN_ENV_KEY} must be a string"
    )
    assert name.strip(), f"{PROJECTION_READBACK_KEY}.{DSN_ENV_KEY} must be non-empty"


@pytest.mark.unit
def test_every_declared_dsn_env_is_a_name_and_not_a_value(
    lanes: dict[str, object],
) -> None:
    """A NAME, never a DSN. This file must never become a credential.

    Checked on every lane rather than only the dev one: the point is that no
    declaration anywhere in this overlay can carry a secret value, and a check
    scoped to the one lane that is supposed to have the block would not see a
    value pasted onto a lane that is not.
    """
    for lane, entry in lanes.items():
        if not isinstance(entry, dict):
            continue
        block = entry.get(PROJECTION_READBACK_KEY)
        if not isinstance(block, dict):
            continue
        name = str(block.get(DSN_ENV_KEY) or "")
        offending = [marker for marker in VALUE_MARKERS if marker in name]
        assert not offending, (
            f"lane {lane!r} declares {DSN_ENV_KEY} containing {offending!r}. "
            "That is a DSN value, not an environment variable name. This file "
            "is config, not secret: declare the NAME the value is injected "
            "under and put the value in the lab store / repo secret of that "
            "name."
        )
        assert ENV_NAME_RE.match(name), (
            f"lane {lane!r} declares {DSN_ENV_KEY}={name!r}, which is not a "
            f"POSIX environment variable name ({ENV_NAME_RE.pattern}). The "
            "workflow injects the secret under this literal name, so a name "
            "the shell cannot export is a readback that silently never runs."
        )


@pytest.mark.unit
def test_no_lane_other_than_dev_declares_a_projection_readback(
    lanes: dict[str, object],
) -> None:
    """Lane scope is enforced here as well as at the consumer.

    The canary publishes a real delegation and then reads a database. Both
    halves are dev-lane-only by the workflow's own declared scope, and
    stability / judge / prod are read-only surfaces this probe has no ticket
    for. A declaration is the thing a future edit would reach for first, so
    the refusal lives on the declaration too, not only in the reader.
    """
    offenders = sorted(
        lane
        for lane, entry in lanes.items()
        if lane != PROJECTION_READBACK_LANE
        and isinstance(entry, dict)
        and PROJECTION_READBACK_KEY in entry
    )
    assert not offenders, (
        f"lanes {offenders} declare {PROJECTION_READBACK_KEY!r}. Only "
        f"{PROJECTION_READBACK_LANE!r} may: node_chain_canary_effect is a "
        "dev-lane instrument and stability / judge / prod are read-only "
        "surfaces it has no authorization to probe."
    )
