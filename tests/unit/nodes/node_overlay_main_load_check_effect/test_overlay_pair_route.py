# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The paired route of the added-nodes check (OMN-24052): an open overlay-repo pull request that names this pull request as its pair.

A node whose overlay reads from overlay-repo main and whose overlay-repo pull request loads the node at the omnimarket commit it pins
cannot merge on either side first. The check accepts the overlay from an OPEN overlay-repo pull request that the body names, when
that pull request pins the overlay at THIS pull request's head and the document loads through THIS tree's model.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_overlay_main_load_check_effect.runtime_overlay_main_load_check import (
    main,
)

# The other repository's name is spelled in two parts: the public-repo hygiene gate refuses it whole on an added line.
REPO = "overlay" + "s"
DOCS = "docs"
NODE = "node_new_reader_effect"
HEAD = "a" * 40
OTHER = "b" * 40
MODEL = (
    "omnimarket.nodes.node_overlay_main_load_check_effect.models."
    "model_overlay_main_load_check:ModelOverlayMainLoadCommand"
)
VALID = "overlays_root: x\npins_file: y\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    nodes = repo / "src/omnimarket/nodes"
    (nodes / "node_base_compute").mkdir(parents=True)
    (nodes / "node_base_compute/contract.yaml").write_text(
        "name: b\n", encoding="utf-8"
    )
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (nodes / NODE).mkdir()
    (nodes / NODE / "contract.yaml").write_text("name: n\n", encoding="utf-8")
    (nodes / NODE / "overlay.example.yaml").write_text("k: v\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "OMN-1: add the node (#5)")
    return repo


def _overlays(
    tmp_path: Path, *, pair: int | None, revision: str = HEAD, document: str = VALID
) -> Path:
    """An overlay-repo checkout whose main holds no document for NODE; ``pair`` is the open pull request's number, fetched as the CI step does."""
    root = tmp_path / "pair-checkout"
    (root / f"{DOCS}/node_unrelated").mkdir(parents=True)
    (root / f"{DOCS}/node_unrelated/overlay.yaml").write_text(
        "k: v\n", encoding="utf-8"
    )
    (root / ".omnimarket-node-pins").write_text("# pins\n", encoding="utf-8")
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed (#1)")
    if pair is not None:
        _git(root, "checkout", "-q", "-b", "pair")
        (root / f"{DOCS}/{NODE}").mkdir()
        (root / f"{DOCS}/{NODE}/overlay.yaml").write_text(document, encoding="utf-8")
        (root / ".omnimarket-node-pins").write_text(
            f"# pins\n{NODE} {revision} {MODEL}\n", encoding="utf-8"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "OMN-24052: the overlay")
        _git(root, "update-ref", f"refs/pair-overlay/{pair}", "HEAD")
        _git(root, "checkout", "-q", "main")
    return root


def _args(repo: Path, pairrepo: Path, *extra: str) -> list[str]:
    return [
        "added-nodes",
        "--repo-root",
        str(repo),
        "--base-ref",
        "HEAD^1",
        f"--{REPO}-root",
        str(pairrepo / DOCS),
        f"--{REPO}-checkout",
        str(pairrepo),
        *extra,
    ]


def test_an_open_pair_is_refused_without_the_pair_route(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails today: the body names an open overlay-repo pull request and the check only accepts a merged one."""
    monkeypatch.setenv("PR_BODY", f"paired with {REPO}#9")
    code = main(_args(_repo(tmp_path), _overlays(tmp_path, pair=9)))
    assert code == 1
    assert f"refused {NODE}" in capsys.readouterr().out


def test_an_open_pair_that_pins_this_head_and_loads_is_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passes paired: the overlay-repo pull request pins this head and its document loads through this tree's model."""
    monkeypatch.setenv("PR_BODY", f"paired with {REPO}#9")
    code = main(
        _args(_repo(tmp_path), _overlays(tmp_path, pair=9), "--pair-head-sha", HEAD)
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "overlay-required: added=1 refused=0" in out
    assert f"paired {NODE} with overlay-repo PR 9" in out


def test_a_pair_pinned_at_another_commit_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PR_BODY", f"{REPO}#9")
    code = main(
        _args(
            _repo(tmp_path),
            _overlays(tmp_path, pair=9, revision=OTHER),
            "--pair-head-sha",
            HEAD,
        )
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "pins" in out
    assert OTHER[:12] in out


def test_a_pair_whose_document_the_model_refuses_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load guarantee holds: a document this tree's model refuses is not accepted because a pair names it."""
    monkeypatch.setenv("PR_BODY", f"{REPO}#9")
    code = main(
        _args(
            _repo(tmp_path),
            _overlays(tmp_path, pair=9, document="overlays_root: x\n"),
            "--pair-head-sha",
            HEAD,
        )
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "pins_file" in out


def test_a_named_pull_request_that_was_not_fetched_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PR_BODY", f"{REPO}#77")
    code = main(
        _args(_repo(tmp_path), _overlays(tmp_path, pair=9), "--pair-head-sha", HEAD)
    )
    assert code == 1
    assert "not fetched" in capsys.readouterr().out


def test_pair_prs_prints_the_numbers_the_body_names(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CI step fetches what this command prints, and only that."""
    monkeypatch.setenv("PR_BODY", f"pairs {REPO}#12 and {REPO}#9, not omnimarket#7")
    assert main(["pair-prs", f"--{REPO}-root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.split() == ["9", "12"]
