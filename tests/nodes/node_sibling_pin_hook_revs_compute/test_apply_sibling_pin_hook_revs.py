# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Apply operation and command line: the planned hook blocks move in a config text, nothing else does (OMN-22753).

The recorded failure is omniclaude's train night of 2026-11-06: core's pin moved to
dev-20261106133647 while the core hooks stayed at dev-20261106041752, and the core hook floor
refused the pull request ("check-no-utcnow pins dev-20261106041752-8377609527be, older than the
locked core dev-20261106133647-64d60448c3e3").
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_sibling_pin_hook_revs_compute import __main__ as cli
from omnimarket.nodes.node_sibling_pin_hook_revs_compute.handlers import (
    HandlerSiblingPinHookRevs,
    HandlerSiblingPinHookRevsApply,
)
from omnimarket.nodes.node_sibling_pin_hook_revs_compute.models import (
    EnumHookRevsReason,
    ModelHookRevsCandidate,
    ModelSiblingPinHookRevsApplyRequest,
    ModelSiblingPinHookRevsApplyResult,
    ModelSiblingPinHookRevsPlan,
    ModelSiblingPinHookRevsRequest,
)

NAME = "node_sibling_pin_hook_revs_compute"
CORE = "https://github.com/OmniNode-ai/omnibase_core"
PIN = "dev-20261105194015-1adbf46062bd"
HOOK = "dev-20261106041752-8377609527be"
NEWEST = "dev-20261106133647-64d60448c3e3"
MID = "dev-20261106100000-0123456789ab"
EARLY = "dev-20261106000000-0123456789ab"
OLD_HOOK = "dev-20261018024445-b6ee996befa1"
STAMP = re.compile(r"^dev-(?P<stamp>[0-9]{14})-[0-9a-f]{12}$")

CONFIG = f"""repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: {HOOK}
    hooks:
      - id: not-core
  # OMN-21516: the core validators by hook id.
  - repo: {CORE}
    rev: {HOOK}
    hooks:
      - id: check-no-utcnow
  - repo: {CORE}
    rev: {OLD_HOOK}  # held at the old tag on purpose
    hooks:
      - id: check-url-authority
  - repo: {CORE}
    rev: v0.47.46
    hooks:
      - id: check-public-runner-placement
  - repo: {CORE}
    rev: "{HOOK}"
    hooks:
      - id: check-local-paths
"""


def stamp(tag: str) -> str:
    match = STAMP.fullmatch(tag)
    assert match is not None, tag
    return match.group("stamp")


def candidates(
    *tags: str, red: tuple[str, ...] = ()
) -> tuple[ModelHookRevsCandidate, ...]:
    return tuple(ModelHookRevsCandidate(tag=t, green=t not in red) for t in tags)


def apply(
    text: str = CONFIG,
    pin: str = PIN,
    tags: tuple[ModelHookRevsCandidate, ...] | None = None,
    url: str = CORE,
) -> ModelSiblingPinHookRevsApplyResult:
    return HandlerSiblingPinHookRevsApply().handle(
        ModelSiblingPinHookRevsApplyRequest(
            precommit_text=text,
            repository_url=url,
            pin_tag=pin,
            candidates=tags if tags is not None else candidates(NEWEST, HOOK, PIN),
        )
    )


def core_revs(text: str) -> list[str]:
    config = yaml.safe_load(text)
    return [str(r["rev"]) for r in config["repos"] if r["repo"] == CORE]


def passes_the_core_hook_floor(pin: str, text: str) -> bool:
    """omniclaude's two rules on the blocks the pin binds: stamp not older than the pin (floor), never the same literal (first-party-ref)."""
    bound = [
        r for r in core_revs(text) if STAMP.fullmatch(r) and stamp(r) >= stamp(PIN)
    ]
    return all(stamp(r) > stamp(pin) for r in bound)


def changed_lines(before: str, after: str) -> list[int]:
    return [
        n
        for n, (a, b) in enumerate(
            zip(before.splitlines(), after.splitlines(), strict=True)
        )
        if a != b
    ]


def _handler() -> Any:
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    entry = next(
        e
        for e in contract["handler_routing"]["handlers"]
        if e["operation"] == "apply_sibling_pin_hook_revs"
    )
    module = importlib.import_module(entry["handler"]["module"])
    assert entry["input_model"].endswith("ModelSiblingPinHookRevsApplyRequest")
    return getattr(module, entry["handler"]["name"])()


def test_golden_chain_the_contract_routes_apply_to_a_handler_that_moves_the_bound_blocks() -> (
    None
):
    result = _handler().handle(
        ModelSiblingPinHookRevsApplyRequest(
            precommit_text=CONFIG,
            repository_url=CORE,
            pin_tag=PIN,
            candidates=candidates(NEWEST, HOOK, PIN),
        )
    )
    assert result.plan.hook_tag == NEWEST
    assert result.plan.pin_tag == HOOK
    assert result.plan.reason is EnumHookRevsReason.HOOKS_AND_PIN_MOVED
    assert result.hook_revs == (HOOK, OLD_HOOK, "v0.47.46", HOOK)


def test_golden_chain_the_train_night_of_2026_11_06_writes_a_tree_the_floor_accepts() -> (
    None
):
    result = apply()
    # Only the two bound core blocks move; the other repository's block, the old
    # block (and its comment), the release block and every comment stay as written.
    assert core_revs(result.candidate_text) == [NEWEST, OLD_HOOK, "v0.47.46", NEWEST]
    assert changed_lines(CONFIG, result.candidate_text) == [7, 19]
    assert f"rev: {OLD_HOOK}  # held at the old tag on purpose" in result.candidate_text
    assert f'rev: "{NEWEST}"' in result.candidate_text
    assert f"rev: {HOOK}\n    hooks:\n      - id: not-core" in result.candidate_text
    assert result.plan.pin_tag is not None
    assert passes_the_core_hook_floor(result.plan.pin_tag, result.candidate_text)


def test_failing_control_the_train_before_this_node_writes_a_tree_the_floor_refuses() -> (
    None
):
    """The recorded failure: the pin takes the newest tag and no hook block moves."""
    old_pin = NEWEST
    assert not passes_the_core_hook_floor(old_pin, CONFIG)
    result = apply()
    assert result.plan.pin_tag is not None
    assert passes_the_core_hook_floor(result.plan.pin_tag, result.candidate_text)


def test_failing_control_the_apply_tests_read_the_strictly_newer_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A planner that gives the pin the hook tag itself (at-or-newer) is caught by the same check."""

    def at_or_newer(
        self: HandlerSiblingPinHookRevs, request: ModelSiblingPinHookRevsRequest
    ) -> ModelSiblingPinHookRevsPlan:
        return ModelSiblingPinHookRevsPlan(
            hook_tag=NEWEST,
            hook_indexes=(0, 3),
            pin_tag=NEWEST,
            reason=EnumHookRevsReason.HOOKS_AND_PIN_MOVED,
        )

    monkeypatch.setattr(HandlerSiblingPinHookRevs, "handle", at_or_newer)
    result = apply()
    assert result.plan.pin_tag is not None
    assert not passes_the_core_hook_floor(result.plan.pin_tag, result.candidate_text)


def test_a_red_newest_tag_is_passed_over_for_hooks_and_pin() -> None:
    result = apply(tags=candidates(NEWEST, MID, HOOK, PIN, red=(NEWEST,)))
    assert (result.plan.hook_tag, result.plan.pin_tag) == (MID, HOOK)
    assert core_revs(result.candidate_text) == [MID, OLD_HOOK, "v0.47.46", MID]
    assert passes_the_core_hook_floor(HOOK, result.candidate_text)


def test_no_green_tag_under_the_hooks_leaves_the_text_and_the_pin_where_they_were() -> (
    None
):
    result = apply(tags=candidates(NEWEST, HOOK, PIN, red=(NEWEST,)))
    assert result.plan.reason is EnumHookRevsReason.NO_GREEN_TAG_UNDER_HOOKS
    assert result.plan.pin_tag is None
    assert result.candidate_text == CONFIG


def test_hooks_already_ahead_leave_the_text_unchanged_and_the_pin_moves_alone() -> None:
    result = apply(tags=candidates(HOOK, EARLY, PIN))
    assert result.plan.reason is EnumHookRevsReason.PIN_MOVED_HOOKS_ALREADY_AHEAD
    assert result.plan.pin_tag == EARLY
    assert result.candidate_text == CONFIG


def test_no_newer_tag_changes_nothing() -> None:
    result = apply(tags=candidates(PIN))
    assert result.plan.reason is EnumHookRevsReason.NO_NEWER_GREEN_TAG
    assert result.candidate_text == CONFIG


@pytest.mark.parametrize("url", [f"{CORE}.git", f"{CORE}/", f"{CORE}.git/"])
def test_the_repository_url_matches_with_or_without_git_suffix(url: str) -> None:
    assert apply(url=url).hook_revs == (HOOK, OLD_HOOK, "v0.47.46", HOOK)


def test_a_config_with_no_block_of_the_repository_plans_the_pin_alone() -> None:
    other = CONFIG.replace(CORE, "https://github.com/OmniNode-ai/omnibase_infra")
    result = apply(text=other)
    assert result.hook_revs == ()
    assert result.plan.reason is EnumHookRevsReason.PIN_MOVED_NO_BOUND_HOOKS
    assert result.plan.pin_tag == NEWEST
    assert result.candidate_text == other


def test_a_repository_that_only_shares_a_prefix_is_not_the_repository() -> None:
    other = CONFIG.replace(CORE, f"{CORE}_extra")
    assert apply(text=other).hook_revs == ()


def test_crlf_line_endings_survive_the_rewrite() -> None:
    result = apply(text=CONFIG.replace("\n", "\r\n"))
    assert result.candidate_text == apply().candidate_text.replace("\n", "\r\n")


def test_the_apply_models_are_closed() -> None:
    with pytest.raises(ValidationError):
        ModelSiblingPinHookRevsApplyRequest.model_validate(
            {
                "precommit_text": "",
                "repository_url": CORE,
                "pin_tag": PIN,
                "candidates": (),
                "surprise": 1,
            }
        )


def _write(tmp_path: Path) -> Path:
    """A consumer tree: a pyproject pinning core at PIN and the config beside it."""
    (tmp_path / ".pre-commit-config.yaml").write_text(CONFIG)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        f'[tool.uv.sources]\nomnibase-core = {{ git = "{CORE}.git", tag = "{PIN}" }}\n'
    )
    return pyproject


def _argv(pyproject: Path, *extra: str) -> list[str]:
    verdicts = json.dumps([[NEWEST, True], [HOOK, True]])
    return [
        "--pyproject",
        str(pyproject),
        "--distribution",
        "omnibase-core",
        "--candidates",
        verdicts,
        *extra,
    ]


def test_cli_prints_the_plan_and_writes_nothing_without_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = _write(tmp_path)
    assert cli.main(_argv(pyproject)) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["plan"]["hook_tag"] == NEWEST
    assert printed["plan"]["pin_tag"] == HOOK
    assert "candidate_text" not in printed
    assert (tmp_path / ".pre-commit-config.yaml").read_text() == CONFIG


def test_cli_apply_moves_the_planned_blocks_in_place(tmp_path: Path) -> None:
    pyproject = _write(tmp_path)
    assert cli.main(_argv(pyproject, "--apply")) == 0
    written = (tmp_path / ".pre-commit-config.yaml").read_text()
    assert core_revs(written) == [NEWEST, OLD_HOOK, "v0.47.46", NEWEST]
    assert passes_the_core_hook_floor(HOOK, written)


def test_cli_leaves_the_config_alone_for_a_candidate_that_is_not_a_verdict(
    tmp_path: Path,
) -> None:
    pyproject = _write(tmp_path)
    argv = _argv(pyproject, "--apply")
    argv[argv.index("--candidates") + 1] = json.dumps([[NEWEST, "maybe"]])
    with pytest.raises(ValidationError):
        cli.main(argv)
    assert (tmp_path / ".pre-commit-config.yaml").read_text() == CONFIG


def test_cli_names_a_distribution_the_pyproject_does_not_pin(tmp_path: Path) -> None:
    pyproject = _write(tmp_path)
    argv = _argv(pyproject)
    argv[argv.index("--distribution") + 1] = "omnibase-infra"
    with pytest.raises(KeyError, match="omnibase-infra"):
        cli.main(argv)


def test_cli_runs_as_a_module_the_way_the_train_calls_it(tmp_path: Path) -> None:
    pyproject = _write(tmp_path)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            f"omnimarket.nodes.{NAME}",
            *_argv(pyproject, "--apply"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["plan"]["pin_tag"] == HOOK
    written = (tmp_path / ".pre-commit-config.yaml").read_text()
    assert core_revs(written)[0] == NEWEST
