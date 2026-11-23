# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A node added in a pull request is not merged until the overlay it requires is on overlays main (OMN-26490).

The incident: a nightly sender node merged while the overlays repository had no document for it, and the dev runtime refused
the node at its first tick. The fixtures below replay that node with an EMPTY overlays tree (built here, never the real
clone), then the controls: the overlay exists, the node declares its overlay optional, the node needs none, and the escape
hatch (the body names a merged overlays pull request that added the document).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.overlay_store.snapshot_root import FileOverlaySnapshot
from omnimarket.models.overlay_store import ModelOverlayStoreRecord, document_digest
from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.adapter_git_overlay_history import (
    added_node_names,
    merged_pr_adds_overlay,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.handler_overlay_required_check import (
    HandlerOverlayRequiredCheck,
    missing_overlays,
    named_overlay_prs,
    overlay_requirement,
    owner_of,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.models.model_overlay_required_check import (
    ModelOverlayRequiredCommand,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SENDER = "node_test_selector_nightly_sender_scheduled_effect"
BARE_CONTRACT = "name: {name}\nnode_type: effect\n"


def _repo_with_sender(tmp_path: Path) -> Path:
    """A tree holding the real sender node's contract and example overlay, and nothing else."""
    source = REPO_ROOT / "src/omnimarket/nodes" / SENDER
    target = tmp_path / "repo/src/omnimarket/nodes" / SENDER
    target.mkdir(parents=True)
    for name in ("contract.yaml", "overlay.example.yaml"):
        shutil.copy(source / name, target / name)
    return tmp_path / "repo"


def _node(repo: Path, name: str, contract: str, *, example: bool = False) -> None:
    node_dir = repo / "src/omnimarket/nodes" / name
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "contract.yaml").write_text(contract, encoding="utf-8")
    if example:
        (node_dir / "overlay.example.yaml").write_text("key: value\n", encoding="utf-8")


def _empty_overlays(tmp_path: Path) -> Path:
    root = tmp_path / "overlays-main/overlays"
    root.mkdir(parents=True)
    return root


def _command(
    repo: Path, overlays: Path, *nodes: str, body: str = ""
) -> ModelOverlayRequiredCommand:
    return ModelOverlayRequiredCommand(
        repo_root=str(repo),
        overlays_root=str(overlays),
        nodes=tuple(nodes),
        pr_body=body,
    )


def _store_snapshot(tmp_path: Path, overlay_id: str) -> Path:
    """A host's snapshot of the settings store holding one overlay, written the way the store read node writes it."""
    root = tmp_path / "store-snapshot"
    document = {"job_id": "x"}
    now = datetime(2026, 11, 23, tzinfo=UTC)
    FileOverlaySnapshot(root).write(
        ModelOverlayStoreRecord(
            overlay_id=overlay_id,
            digest=document_digest(document),
            document=document,
            source_ref="overlays@main",
            loaded_by="test",
            loaded_at=now,
        ),
        written_at=now,
    )
    return root


def _no_history(_checkout: Path, _pr: int, _overlay_id: str) -> bool:
    return False


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
        text=True,
    )
    return done.stdout


def _overlays_history(tmp_path: Path, overlay_id: str) -> Path:
    """An overlays checkout where merged PR 12 added the document and PR 14 added an unrelated one.

    The document of PR 12 was moved away afterwards, so only the history can show it was added.
    """
    repo = tmp_path / "overlays-git"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "overlays" / overlay_id).mkdir(parents=True)
    (repo / "overlays" / overlay_id / "overlay.yaml").write_text(
        "a: b\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add the overlay (#12)")
    _git(repo, "rm", "-q", "-r", f"overlays/{overlay_id}")
    _git(repo, "commit", "-q", "-m", "move it elsewhere (#13)")
    (repo / "overlays" / "node_other").mkdir(parents=True)
    (repo / "overlays" / "node_other" / "overlay.yaml").write_text(
        "a: b\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "an unrelated overlay (#14)")
    return repo


# --- AC1: the 6569 node against an empty overlays tree ------------------------------------------------------


def test_sender_with_an_empty_overlays_tree_is_refused(tmp_path: Path) -> None:
    repo = _repo_with_sender(tmp_path)
    overlays = _empty_overlays(tmp_path)

    result = HandlerOverlayRequiredCheck(merged_pr_adds=_no_history).handle(
        _command(repo, overlays, SENDER)
    )

    assert result.ok is False
    (finding,) = result.findings
    assert finding.node == SENDER
    assert finding.overlay_id == SENDER
    assert finding.looked_for == str(overlays / SENDER / "overlay.yaml")
    assert "merge the overlay" in finding.detail
    assert "merged overlays pull request" in finding.detail


def test_the_sender_is_detected_from_its_example_file_not_from_a_contract_block(
    tmp_path: Path,
) -> None:
    requirement = overlay_requirement(
        _repo_with_sender(tmp_path) / "src/omnimarket/nodes" / SENDER
    )

    assert requirement is not None
    assert requirement.overlay_id == SENDER
    assert requirement.required is True
    assert requirement.source == "example_file"


# --- AC2: controls ------------------------------------------------------------------------------------------


def test_control_overlay_exists_passes(tmp_path: Path) -> None:
    repo = _repo_with_sender(tmp_path)
    overlays = _empty_overlays(tmp_path)
    (overlays / SENDER).mkdir()
    (overlays / SENDER / "overlay.yaml").write_text("job_id: x\n", encoding="utf-8")

    result = HandlerOverlayRequiredCheck(merged_pr_adds=_no_history).handle(
        _command(repo, overlays, SENDER)
    )

    assert result.ok is True
    assert result.findings == ()
    assert result.judged == (SENDER,)


def test_control_node_declaring_its_overlay_optional_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _node(
        repo,
        "node_optional_overlay_effect",
        "name: node_optional_overlay_effect\noverlay:\n  node: node_optional_overlay_effect\n  required: false\n",
        example=True,
    )

    result = HandlerOverlayRequiredCheck(merged_pr_adds=_no_history).handle(
        _command(repo, _empty_overlays(tmp_path), "node_optional_overlay_effect")
    )

    assert result.ok is True
    assert result.judged == ("node_optional_overlay_effect",)


def test_control_node_with_no_overlay_passes_and_a_contract_block_alone_requires_one(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _node(repo, "node_plain_compute", BARE_CONTRACT.format(name="node_plain_compute"))
    _node(
        repo,
        "node_block_effect",
        "name: node_block_effect\noverlay:\n  node: gate_store\n  roots_env: ONEX_SKILL_OVERLAY_ROOTS\n  fields: [a]\n",
    )
    overlays = _empty_overlays(tmp_path)

    handler = HandlerOverlayRequiredCheck(merged_pr_adds=_no_history)
    plain = handler.handle(_command(repo, overlays, "node_plain_compute"))
    block = handler.handle(_command(repo, overlays, "node_block_effect"))

    assert plain.ok is True
    assert block.ok is False
    # the id a contract block names is the id the runtime reads, not the directory name
    assert block.findings[0].overlay_id == "gate_store"
    assert block.findings[0].looked_for == str(overlays / "gate_store" / "overlay.yaml")


def test_a_malformed_contract_does_not_pass_silently(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _node(repo, "node_broken_effect", "name: [unclosed\n", example=True)

    result = HandlerOverlayRequiredCheck(merged_pr_adds=_no_history).handle(
        _command(repo, _empty_overlays(tmp_path), "node_broken_effect")
    )

    assert result.ok is False


# --- escape hatch ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Adds the node. overlays#12", (12,)),
        ("see OmniNode-ai/overlays#12 and OmniNode-ai/overlays#14", (12, 14)),
        ("https://github.com/OmniNode-ai/overlays/pull/12", (12,)),
        ("omnimarket#12 and an unrelated repo/other#13", ()),
        ("", ()),
    ],
)
def test_the_body_names_overlays_pull_requests_only(
    body: str, expected: tuple[int, ...]
) -> None:
    assert named_overlay_prs(body) == expected


def test_history_verifies_a_merged_pr_that_added_the_document(tmp_path: Path) -> None:
    checkout = _overlays_history(tmp_path, SENDER)

    assert merged_pr_adds_overlay(checkout, 12, SENDER) is True
    # unmerged: no commit on main carries this number
    assert merged_pr_adds_overlay(checkout, 99, SENDER) is False
    # unrelated: merged, but it added a different document
    assert merged_pr_adds_overlay(checkout, 14, SENDER) is False
    assert merged_pr_adds_overlay(checkout, 13, SENDER) is False


def test_escape_hatch_passes_with_a_verified_merged_pr(tmp_path: Path) -> None:
    repo = _repo_with_sender(tmp_path)
    overlays = _empty_overlays(tmp_path)
    checkout = _overlays_history(tmp_path, SENDER)
    handler = HandlerOverlayRequiredCheck(merged_pr_adds=merged_pr_adds_overlay)

    named_merged = handler.handle(
        ModelOverlayRequiredCommand(
            repo_root=str(repo),
            overlays_root=str(overlays),
            overlays_checkout=str(checkout),
            nodes=(SENDER,),
            pr_body="The overlay is overlays#12.",
        )
    )
    named_unmerged = handler.handle(
        ModelOverlayRequiredCommand(
            repo_root=str(repo),
            overlays_root=str(overlays),
            overlays_checkout=str(checkout),
            nodes=(SENDER,),
            pr_body="The overlay is overlays#99.",
        )
    )
    named_unrelated = handler.handle(
        ModelOverlayRequiredCommand(
            repo_root=str(repo),
            overlays_root=str(overlays),
            overlays_checkout=str(checkout),
            nodes=(SENDER,),
            pr_body="The overlay is overlays#14.",
        )
    )

    assert named_merged.ok is True
    assert named_unmerged.ok is False
    assert named_unrelated.ok is False


def test_escape_hatch_without_a_checkout_to_verify_against_is_refused(
    tmp_path: Path,
) -> None:
    result = HandlerOverlayRequiredCheck(merged_pr_adds=merged_pr_adds_overlay).handle(
        _command(
            _repo_with_sender(tmp_path),
            _empty_overlays(tmp_path),
            SENDER,
            body="overlays#12",
        )
    )

    assert result.ok is False


# --- only nodes added in the pull request are judged ------------------------------------------------------------


def test_added_nodes_are_the_new_node_directories_against_the_base(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "git-repo"
    _node(repo, "node_old_effect", BARE_CONTRACT.format(name="node_old_effect"))
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _node(repo, "node_new_effect", BARE_CONTRACT.format(name="node_new_effect"))
    (repo / "src/omnimarket/nodes/node_old_effect/extra.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")

    assert added_node_names(repo, "main") == ("node_new_effect",)


def test_the_backlog_is_listed_by_the_same_code_and_is_not_judged_per_pull_request(
    tmp_path: Path,
) -> None:
    repo = _repo_with_sender(tmp_path)
    _node(repo, "node_plain_compute", BARE_CONTRACT.format(name="node_plain_compute"))
    overlays = _empty_overlays(tmp_path)

    listed = missing_overlays(repo, overlays)

    assert [entry.node for entry in listed] == [SENDER]
    judged = HandlerOverlayRequiredCheck(merged_pr_adds=_no_history).handle(
        _command(repo, overlays, "node_plain_compute")
    )
    assert judged.ok is True


def test_owner_is_the_contract_owner_else_unowned_with_the_adding_ticket(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _node(repo, "node_owned_effect", "name: node_owned_effect\nowner: some lane\n")
    _node(repo, "node_unowned_effect", BARE_CONTRACT.format(name="node_unowned_effect"))

    def first_commit_subject(node: str) -> str:
        return f"OMN-26400: adds {node} (#6569)"

    owned = owner_of(
        repo / "src/omnimarket/nodes/node_owned_effect", first_commit_subject
    )
    unowned = owner_of(
        repo / "src/omnimarket/nodes/node_unowned_effect", first_commit_subject
    )

    assert owned == "some lane"
    assert unowned == "unowned (OMN-26400)"


# --- the settings store (operator ruling: overlays on disk are a bootstrap) -----------------------------------


def test_control_overlay_in_the_store_snapshot_passes_without_overlays_main(
    tmp_path: Path,
) -> None:
    repo = _repo_with_sender(tmp_path)
    overlays = _empty_overlays(tmp_path)
    command = _command(repo, overlays, SENDER).model_copy(
        update={"store_snapshot_root": str(_store_snapshot(tmp_path, SENDER))}
    )

    result = HandlerOverlayRequiredCheck(merged_pr_adds=_no_history).handle(command)

    assert result.ok is True
    assert (
        missing_overlays(repo, overlays, (SENDER,), _store_snapshot(tmp_path, SENDER))
        == ()
    )


def test_store_snapshot_that_does_not_hold_the_overlay_is_refused_and_says_it_was_read(
    tmp_path: Path,
) -> None:
    repo = _repo_with_sender(tmp_path)
    overlays = _empty_overlays(tmp_path)
    snapshot = _store_snapshot(tmp_path, "node_other")
    command = _command(repo, overlays, SENDER).model_copy(
        update={"store_snapshot_root": str(snapshot)}
    )

    result = HandlerOverlayRequiredCheck(merged_pr_adds=_no_history).handle(command)

    assert result.ok is False
    assert (
        f"the settings store snapshot at {snapshot} was read and does not hold it"
        in result.findings[0].detail
    )


def test_a_store_snapshot_file_that_does_not_match_its_stamp_does_not_satisfy(
    tmp_path: Path,
) -> None:
    repo = _repo_with_sender(tmp_path)
    overlays = _empty_overlays(tmp_path)
    snapshot = _store_snapshot(tmp_path, SENDER)
    (snapshot / SENDER / "overlay.yaml").write_text(
        "job_id: tampered\n", encoding="utf-8"
    )

    assert missing_overlays(repo, overlays, (SENDER,), snapshot) != ()


def test_refusal_without_a_store_snapshot_says_the_store_was_not_read(
    tmp_path: Path,
) -> None:
    repo = _repo_with_sender(tmp_path)

    result = HandlerOverlayRequiredCheck(merged_pr_adds=_no_history).handle(
        _command(repo, _empty_overlays(tmp_path), SENDER)
    )

    assert result.ok is False
    assert "the settings store was not read" in result.findings[0].detail
