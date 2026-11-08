# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22478 -- the sibling pin train's hook rev bump, and only that.

Since omniclaude#3207 the nightly sibling pin train also moves the ``rev:`` of
first-party hook blocks in ``.pre-commit-config.yaml``. ``classify_dependency_pin_only``
refuses a diff with that path, so every train pull request owed evidence.
``classify_precommit_hook_rev_pin_train_only`` admits exactly one extra shape:
a ``rev:`` line moving from one dev merge tag to another in a first-party hook
block. The failing controls are the load-bearing half: every other edit to the
same file is still refused.
"""

from __future__ import annotations

import pytest

from omnimarket.occ_content_probe import (
    classify_dependency_pin_only,
    classify_precommit_hook_rev_pin_train_only,
)

pytestmark = pytest.mark.unit

CONFIG = ".pre-commit-config.yaml"
OLD = "dev-20261106041752-8377609527be"
NEW = "dev-20261107224734-3e0da50b8d15"
OTHER = "dev-20261029151319-6e0a5eee541a"

PYPROJECT_BASE = (
    '[project]\nname = "x"\nversion = "1.0.0"\ndependencies = ["omnimarket"]\n'
    '[tool.uv.sources]\nomnimarket = { git = "u", tag = "dev-20261106193236-e3fd6f3ad0b3" }\n'
)
PYPROJECT_HEAD = PYPROJECT_BASE.replace(
    "dev-20261106193236-e3fd6f3ad0b3", "dev-20261107234014-0eb80806f6cc"
)


def _config(
    rev_a: str = OLD, rev_b: str = OLD, third: str = OLD, args: str = "[--repo, x]"
) -> str:
    return (
        "repos:\n"
        "  - repo: https://github.com/OmniNode-ai/omnibase_core\n"
        f"    rev: {rev_a}\n"
        "    hooks:\n"
        "      - id: check-public-lab-selection\n"
        f"        args: {args}\n"
        "  - repo: https://github.com/astral-sh/ruff-pre-commit\n"
        f"    rev: {third}\n"
        "    hooks:\n"
        "      - id: ruff\n"
        "  - repo: https://github.com/OmniNode-ai/omnibase_core\n"
        f"    rev: {rev_b}\n"
        "    hooks:\n"
        "      - id: check-no-utcnow\n"
    )


BASE = _config()
HEAD = _config(NEW, NEW)


def _verdict(
    paths: list[str],
    head: str | None = HEAD,
    base: str | None = BASE,
    pyproject_head: str | None = PYPROJECT_HEAD,
    pyproject_base: str | None = PYPROJECT_BASE,
) -> tuple[bool, str]:
    return classify_precommit_hook_rev_pin_train_only(
        paths,
        pyproject_head=pyproject_head,
        pyproject_base=pyproject_base,
        precommit_head=head,
        precommit_base=base,
    )


def test_train_diff_of_pins_and_hook_revs_is_admitted() -> None:
    ok, reason = _verdict([CONFIG, "pyproject.toml", "uv.lock"])
    assert ok, reason
    assert "first-party hook revs" in reason


def test_hook_rev_only_diff_is_admitted() -> None:
    ok, reason = _verdict([CONFIG], pyproject_head=None, pyproject_base=None)
    assert ok, reason


def test_only_one_of_the_blocks_moving_is_admitted() -> None:
    ok, reason = _verdict([CONFIG], head=_config(NEW, OLD))
    assert ok, reason


def test_control_the_existing_classifier_still_refuses_the_config_path() -> None:
    ok, reason = classify_dependency_pin_only(
        [CONFIG, "pyproject.toml", "uv.lock"],
        pyproject_head=PYPROJECT_HEAD,
        pyproject_base=PYPROJECT_BASE,
    )
    assert not ok
    assert CONFIG in reason


@pytest.mark.parametrize(
    ("label", "head"),
    [
        ("a hook's args reworded", _config(NEW, NEW, args="[--repo, y]")),
        ("a hook added", HEAD + "      - id: another-hook\n"),
        ("a hook removed", HEAD.replace("      - id: check-no-utcnow\n", "")),
        (
            "a repo url changed",
            HEAD.replace(
                "https://github.com/OmniNode-ai/omnibase_core",
                "https://github.com/evil/omnibase_core",
                1,
            ),
        ),
        ("a hook id renamed", HEAD.replace("check-no-utcnow", "check-nothing")),
        ("a first-party rev that is not a dev merge tag", _config("v0.47.47", NEW)),
        ("a rev that is a bare sha", _config("a" * 40, NEW)),
        ("a third-party block's rev moved", _config(NEW, NEW, third=NEW)),
        (
            "a trailing comment added to a rev line",
            HEAD.replace(f"rev: {NEW}\n", f"rev: {NEW}  # bump\n", 1),
        ),
        ("a comment line added", "# note\n" + HEAD),
        ("a blank line added", HEAD.replace("repos:\n", "repos:\n\n", 1)),
        (
            "the rev line re-indented",
            HEAD.replace(f"    rev: {NEW}", f"      rev: {NEW}", 1),
        ),
        ("line endings changed", HEAD.replace("\n", "\r\n")),
    ],
)
def test_control_any_other_config_edit_is_refused(label: str, head: str) -> None:
    ok, reason = _verdict([CONFIG, "pyproject.toml", "uv.lock"], head=head)
    assert not ok, label
    assert CONFIG in reason, reason


def test_control_an_unchanged_config_is_refused() -> None:
    ok, reason = _verdict([CONFIG], head=BASE)
    assert not ok
    assert "no hook rev moved" in reason


@pytest.mark.parametrize(
    "paths",
    [
        [CONFIG, "src/app.py"],
        [CONFIG, "pyproject.toml", "uv.lock", ".github/workflows/ci.yml"],
        [CONFIG, "sub/.pre-commit-config.yaml"],
        [CONFIG, "other.yaml"],
    ],
)
def test_control_a_path_outside_the_pin_set_refuses_the_whole_diff(
    paths: list[str],
) -> None:
    ok, reason = _verdict(paths)
    assert not ok
    assert "beyond the hook revs" in reason


def test_control_a_manifest_change_outside_pin_keys_refuses_the_whole_diff() -> None:
    skewed = PYPROJECT_HEAD + '[project.scripts]\nx = "y:z"\n'
    ok, reason = _verdict([CONFIG, "pyproject.toml", "uv.lock"], pyproject_head=skewed)
    assert not ok
    assert "project.scripts" in reason


@pytest.mark.parametrize(
    "kwargs",
    [
        {"head": None},
        {"base": None},
    ],
)
def test_control_an_unreadable_config_is_refused(kwargs: dict[str, None]) -> None:
    ok, reason = _verdict([CONFIG], pyproject_head=None, pyproject_base=None, **kwargs)
    assert not ok
    assert "unreadable" in reason


def test_control_no_paths_and_no_config_path_are_refused() -> None:
    assert not _verdict([])[0]
    ok, reason = _verdict(["pyproject.toml", "uv.lock"])
    assert not ok
    assert "not among the changed files" in reason


def test_the_unrelated_third_party_rev_swap_alone_is_refused() -> None:
    ok, reason = _verdict(
        [CONFIG],
        head=_config(OLD, OLD, third=OTHER),
        pyproject_head=None,
        pyproject_base=None,
    )
    assert not ok
    assert "first-party" in reason
