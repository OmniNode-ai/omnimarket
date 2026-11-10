# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The install-proof read (OMN-21815, OMN-19998): the declaration is the overlay's, git ancestry is read for real.

Seam with the frozen plugin: ``handoff_row.sh`` passes any first word but ``NO`` as a pass, so every refusal here, an
unreadable declaration included, is asserted to start with ``NO``. Failing control: no overlay root, an empty overlay, a
malformed one, one missing a key and one with a regex that does not compile each refuse; none reads as "requires nothing".
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_pr_handoff_lab_exempt_compute.models.model_install_verdict import (
    ModelInstallChangedFile,
)
from omnimarket.nodes.node_pr_handoff_reads_effect.__main__ import main
from omnimarket.nodes.node_pr_handoff_reads_effect.handlers.handler_install_proof_read import (
    HandlerInstallProofRead,
    git_ancestry,
)
from omnimarket.nodes.node_pr_handoff_reads_effect.models.model_install_proof_read import (
    ModelInstallComment,
    ModelInstallProofReadRequest,
)
from tests.nodes.node_pr_handoff_lab_exempt_compute.declaration_fixture import (
    declaration,
)

NODE = "node_pr_handoff_reads_effect"
OBSERVED = "the unit tick ran and exit 0 was printed"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> dict[str, str]:
    """A repository where ``side`` is a branch that does not hold ``head``, and ``tip`` has ``head`` as an ancestor."""
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f").write_text("1")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "g").write_text("s")
    _git(repo, "add", "g")
    _git(repo, "commit", "-q", "-m", "side")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    (repo / "f").write_text("2")
    _git(repo, "commit", "-q", "-am", "head")
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "f").write_text("3")
    _git(repo, "commit", "-q", "-am", "tip")
    tip = _git(repo, "rev-parse", "HEAD")
    return {"path": str(repo), "base": base, "side": side, "head": head, "tip": tip}


def _write_overlay(root: Path, document: object) -> dict[str, str]:
    target = root / NODE
    target.mkdir(parents=True, exist_ok=True)
    (target / "overlay.yaml").write_text(
        document if isinstance(document, str) else yaml.safe_dump(document)
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return _write_overlay(tmp_path / "overlays", {"host_install": declaration()})


def _request(
    clone: dict[str, str], *names: str, **extra: object
) -> ModelInstallProofReadRequest:
    return ModelInstallProofReadRequest(
        repo="omnimarket",
        head=clone["head"],
        files=tuple(ModelInstallChangedFile(filename=n) for n in names),
        clone=clone["path"],
        **extra,  # type: ignore[arg-type]
    )


def test_golden_chain_the_overlay_declares_ci_only_scripts_exempt(
    clone: dict[str, str], env: dict[str, str]
) -> None:
    result = HandlerInstallProofRead(env).handle(
        _request(clone, "scripts/ci/check.py", "scripts/validation/baseline.txt")
    )
    assert result.line.startswith("NA no changed file is a host install")
    assert not result.refused


def test_golden_chain_a_unit_file_with_a_proof_naming_a_descendant_passes(
    clone: dict[str, str], env: dict[str, str]
) -> None:
    body = f"Lab: head={clone['head'][:12]} host=h051 installed=h051:{clone['tip'][:12]} observed={OBSERVED}"
    result = HandlerInstallProofRead(env).handle(
        _request(clone, "launchd/ai.example.plist", pr_body=body)
    )
    assert (
        result.line
        == f"OK installed=h051:{clone['tip'][:12]} source=body contains-head={clone['head'][:7]}"
    )


def test_golden_chain_a_status_row_in_the_ledger_passes(
    clone: dict[str, str], env: dict[str, str], tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        f"t | STATUS | lane=mine | installed=h051:{clone['head'][:12]} | observed={OBSERVED}\n"
    )
    result = HandlerInstallProofRead(env).handle(
        _request(clone, "hooks/pre.sh", lane="mine", ledger_path=str(ledger))
    )
    assert result.status == "OK"


def test_golden_chain_a_comment_of_the_write_node_counts_and_a_stranger_does_not(
    clone: dict[str, str], env: dict[str, str]
) -> None:
    body = f"Lab: head={clone['head'][:12]} installed=h051:{clone['head'][:12]} observed={OBSERVED}"
    comments = (ModelInstallComment(login="a-stranger", body=body),)
    refused = HandlerInstallProofRead(env).handle(
        _request(
            clone,
            "hooks/pre.sh",
            pr_author="author",
            write_logins=("writer-app",),
            comments=comments,
        )
    )
    assert refused.status == "NO"
    counted = HandlerInstallProofRead(env).handle(
        _request(
            clone,
            "hooks/pre.sh",
            pr_author="author",
            write_logins=("writer-app",),
            comments=(ModelInstallComment(login="app/writer-app[bot]", body=body),),
        )
    )
    assert counted.status == "OK"


def test_error_chain_a_unit_file_with_no_proof_is_a_no(
    clone: dict[str, str], env: dict[str, str]
) -> None:
    result = HandlerInstallProofRead(env).handle(
        _request(clone, "systemd/unit.service")
    )
    assert result.status == "NO"
    assert not result.refused
    assert result.required_files == ("systemd/unit.service",)


def test_error_chain_other_scripts_still_need_proof(
    clone: dict[str, str], env: dict[str, str]
) -> None:
    assert (
        HandlerInstallProofRead(env).handle(_request(clone, "scripts/other.sh")).status
        == "NO"
    )


def test_error_chain_a_revision_on_another_branch_does_not_contain_the_head(
    clone: dict[str, str], env: dict[str, str]
) -> None:
    body = f"Lab: head={clone['head'][:12]} installed=h051:{clone['side'][:12]} observed={OBSERVED}"
    result = HandlerInstallProofRead(env).handle(
        _request(clone, "hooks/pre.sh", pr_body=body)
    )
    assert result.status == "NO"
    assert "does not contain it" in result.detail


def test_error_chain_a_revision_the_clone_lacks_is_unread_never_a_pass(
    clone: dict[str, str], env: dict[str, str]
) -> None:
    body = (
        f"Lab: head={clone['head'][:12]} installed=h051:{'f' * 12} observed={OBSERVED}"
    )
    result = HandlerInstallProofRead(env).handle(
        _request(clone, "hooks/pre.sh", pr_body=body)
    )
    assert result.status == "NO"
    assert "its ancestry is UNREAD" in result.detail


def test_git_ancestry_reads_yes_no_and_unread(
    clone: dict[str, str], tmp_path: Path
) -> None:
    assert git_ancestry(clone["path"], clone["tip"], clone["head"]).verdict == "yes"
    assert git_ancestry(clone["path"], clone["side"], clone["head"]).verdict == "no"
    assert git_ancestry(clone["path"], clone["tip"], "9" * 40).verdict == "unread"
    assert (
        git_ancestry(str(tmp_path / "no-clone"), clone["tip"], clone["head"]).verdict
        == "unread"
    )


def test_the_ancestry_reader_is_asked_for_each_named_revision_once(
    clone: dict[str, str], env: dict[str, str]
) -> None:
    asked: list[str] = []

    def reader(path: str, rev: str, head: str):  # type: ignore[no-untyped-def]
        asked.append(rev)
        return git_ancestry(path, rev, head)

    line = f"Lab: head={clone['head'][:12]} installed=h051:{clone['tip'][:12]} observed={OBSERVED}"
    HandlerInstallProofRead(env, reader).handle(
        _request(clone, "hooks/pre.sh", pr_body=line + "\n" + line)
    )
    assert asked == [clone["tip"][:12]]


@pytest.mark.parametrize(
    "case",
    [
        "no-root",
        "no-file",
        "empty",
        "not-yaml",
        "missing-key",
        "bad-regex",
        "unknown-key",
        "empty-paths",
    ],
)
def test_error_chain_an_unreadable_overlay_refuses_and_never_passes(
    case: str, clone: dict[str, str], tmp_path: Path
) -> None:
    """The failing control of the overlay move: every way the declaration can be unreadable is a NO, even for a PR
    that changes nothing that runs on a host and even for an exempt kind."""
    root = tmp_path / "overlays"
    good = declaration()
    if case == "no-root":
        env = {"ONEX_SKILL_OVERLAY_ROOTS": ""}
    elif case == "no-file":
        root.mkdir()
        env = {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}
    elif case == "empty":
        env = _write_overlay(root, "")
    elif case == "not-yaml":
        env = _write_overlay(root, "host_install: [unclosed")
    elif case == "missing-key":
        del good["excluded_paths"]
        env = _write_overlay(root, {"host_install": good})
    elif case == "bad-regex":
        good["observed_refused"] = ["("]
        env = _write_overlay(root, {"host_install": good})
    elif case == "unknown-key":
        good["extra"] = 1
        env = _write_overlay(root, {"host_install": good})
    else:
        good["paths"] = []
        env = _write_overlay(root, {"host_install": good})
    for extra in ({}, {"lab_exempt_kind": "pin-refresh"}):
        result = HandlerInstallProofRead(env).handle(
            _request(clone, "docs/readme.md", **extra)
        )
        assert result.refused
        assert result.line.startswith("NO ")
        assert "host_install declaration cannot be read" in result.detail


def test_refusal_names_no_document_value(clone: dict[str, str], tmp_path: Path) -> None:
    good = declaration()
    good["ruling"] = ""
    good["prod_paths"] = ["a-private-looking-value", ""]
    result = HandlerInstallProofRead(
        _write_overlay(tmp_path / "o", {"host_install": good})
    ).handle(_request(clone, "x"))
    assert result.refused
    assert "a-private-looking-value" not in result.detail


def test_cli_prints_one_line_for_each_verdict_and_refuses_invalid_json(
    clone: dict[str, str],
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    request = _request(clone, "launchd/ai.example.plist")
    monkeypatch.setattr("sys.stdin", io.StringIO(request.model_dump_json()))
    assert main() == 0
    out = capsys.readouterr().out
    assert out.startswith("NO this PR changes what runs on a host")
    assert out.count("\n") == 1
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"repo": "x"})))
    assert main() == 2
