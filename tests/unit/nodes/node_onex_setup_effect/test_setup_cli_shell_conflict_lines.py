# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26614: the shell-safety conflict list rendered as one plain line per conflict.

The human (non---json) setup output prints these lines instead of the raw JSON blob
the shell-safety step stores in its evidence; the blob itself is unchanged for the
--json consumers. Evidence that is not the structured conflict list renders as None,
so the caller prints the value verbatim and display never crashes the walk.
"""

import json

import pytest

from omnimarket.nodes.node_onex_setup_effect import cli_workflow


def _render(value: object) -> object:
    """Resolve the renderer at call time so collection works on older code.

    A missing renderer is a behavioural failure of this test, not an import
    error during collection.
    """
    renderer = getattr(cli_workflow, "shell_conflict_lines", None)
    assert callable(renderer), "cli_workflow.shell_conflict_lines is missing"
    return renderer(value)


def _row(
    kind: str,
    location: str = "current environment",
    name: str = "OMNI_HOME",
    line: int | None = None,
    recoverable: bool = False,
) -> dict[str, object]:
    return {
        "kind": kind,
        "location": location,
        "line": line,
        "name": name,
        "recoverable": recoverable,
    }


def test_each_kind_renders_location_name_and_remedy() -> None:
    lines = _render(
        json.dumps(
            [
                _row("profile", ".bashrc", "KAFKA_BROKERS", 2, True),
                _row("environment"),
                _row("executable", "PATH", "onex"),
                _row("unsafe-profile", ".bashrc", "regular readable profile required"),
            ]
        )
    )
    assert lines == [
        ".bashrc line 2: KAFKA_BROKERS is set by this profile line; "
        "--set-aside preserves and disables it when setup runs again.",
        "current environment: OMNI_HOME is inherited from the current environment; "
        "resolve it by hand.",
        "PATH: another onex executable resolves first on PATH; resolve it by hand.",
        ".bashrc: this profile is not a regular readable file; resolve it by hand.",
    ]


def test_non_recoverable_profile_line_says_by_hand() -> None:
    lines = _render(json.dumps([_row("profile", ".bashrc", "KAFKA_BROKERS", 3, False)]))
    assert lines == [
        ".bashrc line 3: KAFKA_BROKERS is set by this profile line; resolve it by hand."
    ]


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        json.dumps([{"kind": "no-such-kind", "location": "x", "name": "y"}]),
        json.dumps([]),
    ],
)
def test_undecodable_or_unknown_evidence_falls_back_to_verbatim(value: str) -> None:
    """EXTRA1: evidence that is not the structured conflict list renders as None."""
    assert _render(value) is None
