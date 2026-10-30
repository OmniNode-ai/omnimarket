# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A synthetic deployment host table for the pre-push tests (OMN-20939).

The shipped ``scripts/hooks/prepush_hosts.tsv`` carries only a neutral disabled
row; a deployment's real hosts live in its own private table. These tests need
rows to exercise the picker and the identity guard, so they build a table of the
same fifteen-column shape from invented hosts (RFC 5737 documentation
addresses, ``.invalid`` names) and point the workspace-config resolver at it.
The labels keep the names the picker's override maps use; no value here belongs
to any real machine.
"""

from __future__ import annotations

from pathlib import Path

PRIVATE_TABLE_REL = "config/onex/overlays/prepush_hosts/prepush_hosts.omnimarket.tsv"

_HEADER = "#label\trole\thostname\tssh_target\tcores\tuv_abs_path\tuv_min_version\tworkroot\tslot_mode\tslots\trepos_denied\tmode\theavy_local\tplacement_tier\tnote\n"
HOST_A = "gate-host-a"
HOST_B = "gate-host-b"
HOST_B_CONTAINER = "gate-runner-b"
HOST_C = "gate-host-c"
HOST_D = "gate-host-d"
HOST_E = "gate-host-e"


def _row(
    label: str,
    hostname: str,
    target: str,
    *,
    slots: int = 1,
    role: str = "capacity",
    policy: str = "allowed",
    tier: str = "default",
    denied: str = "-",
) -> str:
    return (
        "\t".join(
            (
                label,
                role,
                hostname,
                target,
                "24",
                "/opt/synthetic/bin/uv" if role == "capacity" else "-",
                "0.8.3" if label == "h101" else "0.11.0" if role == "capacity" else "-",
                "/srv/onex-prepush" if role == "capacity" else "-",
                "queue"
                if label == "h201"
                else "lockdir"
                if role == "capacity"
                else "none",
                str(slots),
                denied,
                "authorizing",
                policy,
                tier,
                "Synthetic deployment fixture",
            )
        )
        + "\n"
    )


SYNTHETIC_ROWS = (
    _row("h200", HOST_A, "gate-host-a.invalid", policy="prefer_remote")
    + _row(
        "h201",
        HOST_B,
        "192.0.2.21",
        slots=3,
        policy="prefer_remote",
        tier="last_resort",
    )
    + _row(
        "h201c",
        HOST_B_CONTAINER,
        "-",
        role="identity",
        policy="prefer_remote",
        tier="-",
    )
    + _row("h101", HOST_C, "192.0.2.31", slots=1, denied="omnimarket")
    + _row("h105", HOST_D, "192.0.2.41", slots=1, denied="omnimarket")
    + _row("h051", HOST_E, "192.0.2.51", slots=4)
)
SYNTHETIC_TABLE = _HEADER + SYNTHETIC_ROWS


def write_private_root(base: Path, table_text: str = SYNTHETIC_TABLE) -> Path:
    """Create a plain workspace-config directory with invented deployment rows."""
    root = base / "workspace_config"
    target = root / PRIVATE_TABLE_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(table_text, encoding="utf-8")
    return root
