# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""host_profile_derive: a host declared once derives rows equal to the existing declarations' rows."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.handler_host_profile import (
    HandlerHostProfileCompute,
    parse_host_profile,
)
from omnimarket.models.model_host_profile import (
    ModelHostProfile,
    ModelHostProfileDerived,
    ModelHostProfileDeriveRequest,
)

# The pool host table's clones column for host-b, as declared before the profile existed.
EXISTING_HOST_B_CLONES = [("Example-Org/market", "dev", "$OMNI_HOME/market")]
EXISTING_HOST_A_CLONES = [
    ("Example-Org/market", "dev", "$OMNI_HOME/market"),
    ("Example-Org/infra", "dev", "$OMNI_HOME/infra"),
    ("Example-Org/shared_docs", "main", "/srv/shared/docs"),
]

# Existing pre-push rows of host-a, free-text note dropped: label .. mode, then the extra columns.
_PRE = "host-a\tcapacity\thost-a-name\thost-a\t16\t/usr/local/bin/uv\t0.11.0\t/srv/prepush\tlockdir"
EXISTING_HOST_A_PREPUSH = {
    "prepush_hosts.core.tsv": f"{_PRE}\t4\t-\tauthorizing\t",
    "prepush_hosts.infra.tsv": f"{_PRE}\t2\t-\tauthorizing\tallowed\tdefault\t",
    "prepush_hosts.market.tsv": f"{_PRE}\t2\t-\tauthorizing\tallowed\tdefault\t",
}

# host-a's existing reconcile liveness entry.
EXISTING_HOST_A_RECONCILE = {
    "process_id": "host-a/reconcile",
    "owner_repo": "market",
    "host": "host-a",
    "trigger": {"kind": "systemd-timer", "native_id": "onex-reconcile.timer"},
    "expected_interval_seconds": 3600,
    "max_runtime_seconds": 4200,
    "alarm_deadline_seconds": 7620,
    "emitter": "observer",
    "run_record": "per-run",
    "evidence": {
        "source": "log_line",
        "locator": "$OMNI_HOME/.state/reconcile.out.log",
        "completion_record": "one JSON line with exit_code",
    },
    "real_work": "surfaces reconciled",
    "demand": "none",
    "positive_control": {"kind": "class_drill", "ref": "class-drill/systemd-timer"},
    "state": "active",
}

EXAMPLE_PROFILES = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_host_profile_compute/host_profiles.example.yaml"
)


def load_host(host: str) -> ModelHostProfile:
    return parse_host_profile(EXAMPLE_PROFILES.read_text(), host)


def derive(host: str) -> ModelHostProfileDerived:
    profile = load_host(host)
    return HandlerHostProfileCompute().handle(
        ModelHostProfileDeriveRequest(profile=profile)
    )


def test_host_profile_derive_clone_rows_equal_existing_declaration() -> None:
    assert [
        (c.repo, c.ref, c.path) for c in derive("host-b").clone_rows
    ] == EXISTING_HOST_B_CLONES
    rows = [(c.repo, c.ref, c.path) for c in derive("host-a").clone_rows]
    assert rows == EXISTING_HOST_A_CLONES


def test_host_profile_derive_clone_outside_the_pool_table_names_its_reader() -> None:
    derived = derive("host-a")
    extra = {c.repo for c in derived.clones} - {c.repo for c in derived.clone_rows}
    assert extra == {"Example-Org/core", "Example-Org/graph"}
    profile = load_host("host-a")
    assert all(c.reader for c in profile.clones if not c.lab_run_row)


def test_host_profile_derive_clone_without_reader_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelHostProfile.model_validate(
            {
                "host": "x",
                "registry_root": "/o",
                "python": "3.12",
                "git_remote_template": "u/{repo}",
                "clones": [
                    {"repo": "a/b", "ref": "dev", "path": "/p", "lab_run_row": False}
                ],
            }
        )


def test_host_profile_derive_venvs_use_the_host_interpreter_per_clone() -> None:
    venvs = derive("host-a").venvs
    assert [v.venv_path for v in venvs] == [
        "/srv/registry/core/.venv",
        "/srv/registry/market/.venv",
    ]
    assert {v.python for v in venvs} == {"3.12"}


def test_host_profile_derive_prepush_rows_equal_existing_rows() -> None:
    derived = derive("host-a")
    assert derived.prepush_workroot == "/srv/prepush"
    assert {r.file: r.line for r in derived.prepush_rows} == EXISTING_HOST_A_PREPUSH
    assert {r.file: r.target for r in derived.prepush_rows} == {
        f: f"/srv/registry/.state/prepush_hosts/{f}" for f in EXISTING_HOST_A_PREPUSH
    }


def test_host_profile_derive_liveness_row_equals_existing_entry() -> None:
    rows = {r["process_id"]: r for r in derive("host-a").liveness_rows}
    assert rows["host-a/reconcile"] == EXISTING_HOST_A_RECONCILE


def test_host_profile_derive_second_host_liveness_row_differs_only_by_host() -> None:
    rows = {r["process_id"]: r for r in derive("host-b").liveness_rows}
    expected = {
        **EXISTING_HOST_A_RECONCILE,
        "process_id": "host-b/reconcile",
        "host": "host-b",
    }
    assert rows["host-b/reconcile"] == expected


def test_host_profile_derive_host_without_a_prepush_block_derives_no_prepush_rows() -> (
    None
):
    derived = derive("host-b")
    assert derived.prepush_rows == ()
    assert derived.prepush_workroot is None


def test_host_profile_derive_unknown_host_is_refused() -> None:
    with pytest.raises(KeyError):
        load_host("h999")
