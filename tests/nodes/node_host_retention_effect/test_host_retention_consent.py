# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the operator's archive consent, the overlay model and the root a host row declares.

node_retention_cleanup's policy names the folders the operator consented to have archived, never deleted. A lane_results class
of a host row on one of those folders that does not archive is refused by the model check before the pass reads or removes
anything. Every test has its control: the same class with ``archive: true`` (or on an unrelated folder) is not refused. A terminal
guard must carry its receipt evidence, or an old lane the ledger window does not hold is kept forever.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_host_retention_effect.handlers.handler_host_retention import (
    consent_archive_roots,
)
from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelAgeClass,
    ModelHostRetention,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_file_retention import (
    load_file_policy,
)
from tests.nodes.node_host_retention_effect.host_retention_fakes import (
    DAY,
    NOW,
    FakeArchive,
    FakeClosure,
    entry,
    make_ports,
    results_class,
    run,
)

pytestmark = pytest.mark.unit


def consent_row(root: str, *, archive: bool) -> ModelHostRetention:
    """A host row whose only class is a lane_results class on ``root``."""
    return ModelHostRetention.model_validate(
        {
            "name": "pool-a",
            "docker_timeout_seconds": 5,
            "age_classes": [
                {
                    "name": "state",
                    "kind": "lane_results",
                    "root": root,
                    "keep_days": 7,
                    "archive": archive,
                }
            ],
        }
    )


def archive_entry_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[tuple[str, str]]:
    """(entry name, expanded path) of every archive entry of the cleanup policy, under a pinned home."""
    monkeypatch.setenv("HOME", str(tmp_path.resolve()))
    monkeypatch.setenv("OMNI_HOME", str(tmp_path.resolve() / "omni"))
    return [
        (e.name, os.path.expandvars(os.path.expanduser(e.path)))
        for e in load_file_policy().entries
        if e.action == "archive"
    ]


def test_consent_seam_every_archive_entry_of_the_policy_is_a_root_a_plain_delete_is_refused_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = archive_entry_roots(monkeypatch, tmp_path)
    consent = consent_archive_roots()

    assert roots, "the cleanup policy declares archive entries"
    for name, path in roots:
        refused = consent_row(path, archive=False).consent_refusals(consent)
        assert len(refused) == 1, name
        assert "archive: true" in refused[0], name
        assert consent_row(path, archive=True).consent_refusals(consent) == [], (
            f"{name}: control, archived is accepted"
        )


def test_consent_seam_the_two_lane_state_folders_are_named_by_the_policy_even_while_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = dict(archive_entry_roots(monkeypatch, tmp_path))

    assert roots["remote-lane-receipts"].endswith("/.local/state/omni/remote-lanes")
    assert roots["lab-run-lane-dirs"].endswith("/lab-run/runs")
    disabled = {e.name for e in load_file_policy().entries if not e.enabled}
    assert {"remote-lane-receipts", "lab-run-lane-dirs"} <= disabled, (
        "the entries are disabled because this node owns the folders; the consent holds"
    )
    consent = consent_archive_roots()
    for name in ("remote-lane-receipts", "lab-run-lane-dirs"):
        assert os.path.realpath(roots[name]) in consent, name


def test_consent_seam_control_a_folder_the_consent_does_not_name_may_be_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_entry_roots(monkeypatch, tmp_path)
    unrelated = str(tmp_path.resolve() / "elsewhere" / "harness-delegate")

    assert (
        consent_row(unrelated, archive=False).consent_refusals(consent_archive_roots())
        == []
    )


def test_a_pass_on_a_consent_root_without_archive_is_refused_and_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    root = home / ".local" / "state" / "omni" / "remote-lanes"
    root.mkdir(parents=True)
    old = entry(root, "lane-omega", days=10)
    plain = {"age_classes": [results_class(root, keep_days=7)]}

    result = run(
        tmp_path,
        plain,
        ports=make_ports(closure=FakeClosure({"omega": NOW - 10 * DAY})),
    )

    assert result.status == "refused"
    assert result.items == ()
    assert old.exists()
    assert any("archive: true" in text for text in result.refusals), result.refusals


def test_control_the_same_pass_with_archive_is_accepted_and_archives_before_it_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    monkeypatch.setenv("HOME", str(home))
    root = home / ".local" / "state" / "omni" / "remote-lanes"
    root.mkdir(parents=True)
    old = entry(root, "lane-omega", days=10)
    archived = {
        "age_classes": [
            results_class(
                root,
                keep_days=7,
                archive=True,
                lane_pattern="lane-(?P<lane>[a-z]+)",
            )
        ]
    }
    archive = FakeArchive(tmp_path / "archive")

    result = run(
        tmp_path,
        archived,
        ports=make_ports(
            closure=FakeClosure({"omega": NOW - 10 * DAY}), archive=archive
        ),
    )

    assert result.status == "ok"
    assert not old.exists()
    assert [source for source, _dest in archive.archived] == [old]


def test_a_terminal_guard_without_receipt_evidence_is_refused_by_the_model(
    tmp_path: Path,
) -> None:
    fields: dict[str, Any] = results_class(tmp_path / "remote-lanes")
    del fields["terminal"]["receipt"]

    with pytest.raises(ValidationError) as caught:
        ModelAgeClass.model_validate(fields)

    assert "receipt" in str(caught.value)
    fields["terminal"]["receipt"] = {"root": str(tmp_path), "glob": "{lane}/*.json"}
    assert ModelAgeClass.model_validate(fields).terminal is not None, "control"
