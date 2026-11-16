# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lookup from a lab host whose overlays clone is not under the lane's overlay root (OMN-24916).

A lane's ``ONEX_SKILL_OVERLAY_ROOTS`` named ``$OMNI_HOME/overlays/overlays`` on a host whose overlays
clone the lab host table declares beside the registry root, so the lookup found no overlay (exit 3).
``--host`` reads the overlay from the clone the table declares; the LOOKUP line carries the answered
commit's time and the read time; ``--retention-versions`` is the caller's lookup block where the
overlay carries none.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_code_graph_effect import runtime_code_graph
from omnimarket.nodes.node_code_graph_effect.adapters.version_stores import (
    InMemoryVersionStore,
)
from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import main
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    CORE,
    EXAMPLE,
    _git,
    commit,
    make_clone,
)

pytestmark = pytest.mark.unit

STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
NODE_OVERLAY = "node_code_graph_effect/overlay.yaml"
HOST_TABLE = {
    "hosts": [
        {
            "name": "h9",
            "clones": [
                {
                    "repo": "OmniNode-ai/overlays",
                    "ref": "main",
                    "path": "$OMNI_HOME/../overlays",
                }
            ],
        }
    ]
}


def _install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lookup: bool = True,
    table: str | bytes | None = yaml.safe_dump(HOST_TABLE),
    table_under_roots: bool = False,
    node_overlay: bool = True,
) -> Path:
    """A registry root whose overlays clone sits beside it, declared for host h9 only.

    ``table`` is the lab host table's text (``None`` writes no table). It sits in the overlays clone
    beside the registry root, or, with ``table_under_roots``, only under a root named in
    ``ONEX_SKILL_OVERLAY_ROOTS``. ``node_overlay=False`` leaves the declared clone without the node's
    overlay.
    """
    omni_root = tmp_path / "registry_root"
    omni_root.mkdir()
    clones = tmp_path / "clones"
    make_clone(clones, "example_core", CORE)
    make_clone(clones, "example_app", APP)
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["graph_backend"] = "git"
    if not lookup:
        del document["lookup"]
    overlays = tmp_path / "overlays" / "overlays"
    overlays.mkdir(parents=True)
    if node_overlay:
        target = overlays / NODE_OVERLAY
        target.parent.mkdir(parents=True)
        target.write_text(yaml.safe_dump(document), encoding="utf-8")
    skill_roots = tmp_path / "skill_roots"
    lane_root = omni_root / "overlays" / "overlays"
    if table is not None:
        table_root = skill_roots if table_under_roots else overlays
        (table_root / "lab_hosts").mkdir(parents=True)
        path = table_root / "lab_hosts" / "overlay.yaml"
        if isinstance(table, bytes):
            path.write_bytes(table)
        else:
            path.write_text(table, encoding="utf-8")
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", str(skill_roots if table_under_roots else lane_root)
    )
    monkeypatch.setenv("OMNI_HOME", str(omni_root))
    monkeypatch.setenv("EXAMPLE_REPOS_ROOT", str(clones))
    monkeypatch.delenv("EXAMPLE_HOST_NAME", raising=False)
    runtime_code_graph._VERSIONS.clear()
    return omni_root


def _lookup(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str, str]:
    code = main(
        [
            "lookup",
            "--question",
            "importers",
            "--repo",
            "example_core",
            "--path",
            "src/example_core/models.py",
            *extra,
        ]
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _on_host(
    capsys: pytest.CaptureFixture[str], omni_root: Path, *extra: str
) -> tuple[int, str, str]:
    """The lookup as ``--host h9`` with the registry root named, the only place the flag is passed."""
    return _lookup(capsys, "--workspace-root", str(omni_root), "--host", "h9", *extra)


def _clone_time(
    tmp_path: Path, revision: str = "HEAD", repo: str = "example_core"
) -> str:
    """The commit's time as ``git log -1 --format=%ct`` reads it, as the LOOKUP line writes it."""
    seconds = _git(tmp_path / "clones" / repo, "log", "-1", "--format=%ct", revision)
    return datetime.fromtimestamp(int(seconds), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _field(line: str, name: str) -> str:
    found = re.search(rf" {name}=(\S+)", line)
    assert found, line
    return found.group(1)


def _commit_at(
    clone: Path, monkeypatch: pytest.MonkeyPatch, when: str, message: str
) -> str:
    """A new commit of ``example_core`` dated ``when`` (an offset is allowed), the new origin/main."""
    monkeypatch.setenv("GIT_COMMITTER_DATE", when)
    head = commit(
        clone, {"src/example_core/extra.py": f"VALUE = {message!r}\n"}, message
    )
    monkeypatch.delenv("GIT_COMMITTER_DATE")
    _git(clone, "update-ref", "refs/remotes/origin/main", head)
    return head


def test_the_lane_overlay_root_alone_exits_3_without_the_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The unchanged behaviour: no ``--host``, so no flag of this change is passed."""
    _install(tmp_path, monkeypatch)
    code, _, err = _lookup(capsys)
    assert code == 3
    assert "the overlay file is not configured" in err


def test_the_declared_host_clone_answers_with_the_newest_commit_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch)
    code, out, err = _on_host(capsys, omni_root)
    assert code == 0, out + err
    assert "MATCH src/example_core/cli.py\n" in out
    summary = out.splitlines()[-1]
    assert "status=found" in summary
    assert _field(summary, "committed_at") == _clone_time(tmp_path)
    assert (
        _field(summary, "answered_from")
        == _git(tmp_path / "clones" / "example_core", "rev-parse", "HEAD")[:12]
    )
    assert re.search(rf" read_at={STAMP} ", summary), summary


def test_the_workspace_root_defaults_to_omni_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(tmp_path, monkeypatch)
    code, out, err = _lookup(capsys, "--host", "h9")
    assert code == 0, out + err
    assert "status=found" in out.splitlines()[-1]


def test_a_relative_workspace_root_names_the_overlays_clone_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch)
    monkeypatch.chdir(omni_root)
    code, out, err = _lookup(capsys, "--workspace-root", ".", "--host", "h9")
    assert code == 0, out + err
    assert "status=found" in out.splitlines()[-1]


def test_committed_at_is_the_commit_time_in_utc_whatever_its_own_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch)
    _commit_at(
        tmp_path / "clones" / "example_core",
        monkeypatch,
        "2026-11-01T12:00:00+0500",
        "offset",
    )
    code, out, err = _on_host(capsys, omni_root)
    assert code == 0, out + err
    committed_at = _field(out.splitlines()[-1], "committed_at")
    assert committed_at == "2026-11-01T07:00:00Z"
    assert committed_at == _clone_time(tmp_path)


def test_as_of_a_stored_older_commit_answers_with_that_commits_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch)
    clone = tmp_path / "clones" / "example_core"
    older = _git(clone, "rev-parse", "HEAD")
    code, out, err = _on_host(capsys, omni_root)
    assert code == 0, out + err
    version = runtime_code_graph._VERSIONS[3].newest("example_core")
    assert version is not None
    _commit_at(clone, monkeypatch, "2027-01-02T03:04:05+0000", "newer")
    runtime_code_graph._VERSIONS.clear()
    code, out, err = _on_host(capsys, omni_root)
    assert code == 0, out + err
    newest = runtime_code_graph._VERSIONS[3].newest("example_core")
    assert newest is not None
    assert newest.commit != older
    # The process now holds both versions, the older one stored first.
    both = InMemoryVersionStore(3)
    both.put(version)
    both.put(newest)
    runtime_code_graph._VERSIONS[3] = both

    code, out, err = _on_host(capsys, omni_root, "--as-of", older)
    assert code == 0, out + err
    summary = out.splitlines()[-1]
    assert _field(summary, "answered_from") == older[:12]
    assert _field(summary, "committed_at") == _clone_time(tmp_path, older)
    assert _field(summary, "committed_at") != _clone_time(tmp_path)

    code, out, err = _on_host(capsys, omni_root)
    assert _field(out.splitlines()[-1], "committed_at") == "2027-01-02T03:04:05Z"


def test_as_of_a_commit_no_version_holds_answers_from_none_with_no_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch)
    code, out, err = _on_host(capsys, omni_root)
    assert code == 0, out + err
    assert _field(out.splitlines()[-1], "committed_at") == _clone_time(tmp_path)
    code, out, err = _on_host(capsys, omni_root, "--as-of", "0" * 40)
    summary = out.splitlines()[-1]
    assert _field(summary, "answered_from") == "none", out + err
    assert _field(summary, "committed_at") == "none"


def test_a_host_the_table_does_not_declare_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch)
    code, out, err = _lookup(capsys, "--workspace-root", str(omni_root), "--host", "h8")
    assert code == 3, out
    assert "h8: the lab host table declares no row for this host" in err


def test_the_caller_lookup_block_answers_where_the_overlay_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch, lookup=False)
    code, out, _ = _on_host(capsys, omni_root)
    assert code == 1
    assert "reason=the overlay has no lookup block" in out
    code, out, err = _on_host(capsys, omni_root, "--retention-versions", "2")
    assert code == 0, out + err
    assert "status=found" in out.splitlines()[-1]
    assert set(runtime_code_graph._VERSIONS) == {2}


def test_the_overlay_lookup_block_wins_over_the_caller_flag_when_both_are_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch)
    overlay_retention = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["lookup"][
        "retention_versions"
    ]
    assert overlay_retention != 7
    code, out, err = _on_host(capsys, omni_root, "--retention-versions", "7")
    assert code == 0, out + err
    assert set(runtime_code_graph._VERSIONS) == {overlay_retention}


def test_a_missing_omni_home_exits_3_with_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(tmp_path, monkeypatch)
    monkeypatch.delenv("OMNI_HOME")
    code, out, err = _lookup(capsys, "--host", "h9")
    assert code == 3, out
    assert "INDETERMINATE: ValueError: OMNI_HOME is not set" in err


def test_retention_versions_zero_exits_3_with_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch, lookup=False)
    code, out, err = _on_host(capsys, omni_root, "--retention-versions", "0")
    assert code == 3, out
    assert "INDETERMINATE: ValidationError" in err
    assert "retention_versions" in err


def test_a_table_that_is_not_valid_yaml_is_refused_with_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch, table="hosts: [unclosed\n  - : :\n")
    code, out, err = _on_host(capsys, omni_root)
    assert code == 3, out
    assert "h9: " in err
    assert "cannot be read as YAML" in err
    assert "Traceback" not in err


def test_a_table_that_cannot_be_decoded_is_refused_with_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch, table=b"\xff\xfe\x00hosts\n")
    code, out, err = _on_host(capsys, omni_root)
    assert code == 3, out
    assert "cannot be read as YAML" in err


def test_no_table_is_refused_with_exit_3_naming_where_it_looked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch, table=None)
    code, out, err = _on_host(capsys, omni_root)
    assert code == 3, out
    assert "h9: no lab_hosts/overlay.yaml under" in err
    assert str(omni_root / "overlays" / "overlays") in err
    assert str(tmp_path / "overlays" / "overlays") in err


def test_a_table_that_is_not_a_mapping_is_refused_with_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch, table="- h9\n- h8\n")
    code, out, err = _on_host(capsys, omni_root)
    assert code == 3, out
    assert "is not a mapping" in err


def test_a_declared_clone_without_the_node_overlay_is_refused_with_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch, node_overlay=False)
    code, out, err = _on_host(capsys, omni_root)
    assert code == 3, out
    assert f"holds no {NODE_OVERLAY}" in err


def test_the_table_is_found_through_the_skill_overlay_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch, table_under_roots=True)
    assert not (tmp_path / "overlays" / "overlays" / "lab_hosts").exists()
    assert not (omni_root / "overlays").exists()
    code, out, err = _on_host(capsys, omni_root)
    assert code == 0, out + err
    assert "status=found" in out.splitlines()[-1]


def test_a_host_other_than_the_one_running_is_refused_with_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    omni_root = _install(tmp_path, monkeypatch)
    monkeypatch.setenv("EXAMPLE_HOST_NAME", "h8")
    code, out, err = _on_host(capsys, omni_root)
    assert code == 3, out
    assert "h9: this process runs as host h8" in err
    monkeypatch.setenv("EXAMPLE_HOST_NAME", "h9")
    code, out, err = _on_host(capsys, omni_root)
    assert code == 0, out + err
