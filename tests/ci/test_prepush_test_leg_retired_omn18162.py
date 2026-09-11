# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pre-push TEST leg stays retired in this repo (OMN-18162).

OMN-18162 removed the governed impacted-test selector -- the hook that wrapped
``scripts/hooks/prepush_smart_tests.sh`` -- from the pre-push stage of
``.pre-commit-config.yaml``. It was the only hook here that invoked a test
runner, so pre-push is now a strict mypy type check and nothing else, and
finishes in seconds. Hosted CI is the enforced merge gate and the test surface.

Plan of record: the CI runner placement and pre-push retirement plan of
2026-09-09, phase 1. Operator ruling 2026-09-10, re-affirmed for this repo
2026-09-11: retire the test leg one repo at a time.

Per root rule 5 (enforcement, not detection) this module is the mechanism, not
a note. A retirement that lives only in a comment is re-added by the next lane
that wants faster local feedback.

Seven properties are asserted, and each one fails a different way of undoing
the change:

1. **No pre-push hook invokes a test runner.** Stated as a property of the
   stage rather than a denylist of one hook id, so re-adding the same behaviour
   under a new id or a new wrapper script is caught too.
2. **The surviving pre-push hook is exactly the mypy type check.** The
   retirement removes one hook and touches nothing else.
3. **The selector script is NOT deleted, and says it is manual-only.** The
   remote lab leg is launched and slot-gated under that file's name and five
   modules under ``tests/scripts/`` pin its content, so deleting it would break
   surfaces this change never intended to touch.
4. **No bypass surface was introduced.** An ambient environment variable is
   inherited by every descendant process and leaves no receipt; the retirement
   must not ship one, and does not need one -- the leg is simply gone.
5. **The retirement opened no coverage gap, and the premise stays true.** The
   pre-push filter was a strict subset of CI's, so narrowing CI's filter later
   is what would open a gap. That is pinned here rather than re-measured by
   hand.
6. **The retirement block does not spell the legacy bootstrap's grep
   literals.** This repo's pre-OMN-17851 pre-push bootstrap greps the
   pre-commit config for the removed hook's id and entry line and treats a
   match as proof that a governed test gate is wired. Writing them into a
   comment would make a stale install read documentation as wiring.
7. **The tracked bootstrap installer carries no such authority check either**,
   so the superseded pattern cannot come back through the installer.

This is a hermetic static scan. It reads files off disk, runs no subprocess and
touches no network.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
SELECTOR_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "prepush_smart_tests.sh"
BOOTSTRAP_INSTALLER = REPO_ROOT / "scripts" / "hooks" / "install_prepush_hook.py"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PRE_PUSH_STAGE = "pre-push"

RETIRED_HOOK_ID = "prepush-smart-tests"

# The pre-push stage after OMN-18162, by hook id, in file order. The plan
# records the pre-change count for this repo as 2 (type check, smart tests).
EXPECTED_PRE_PUSH_HOOK_IDS: tuple[str, ...] = ("mypy-type-check",)

# Tokens that mean "this hook runs a test runner". Matched against a hook's
# resolved command surface -- its entry, plus the script that entry names when
# the script lives in this repo.
_TEST_RUNNER_RE = re.compile(r"\b(pytest|py\.test|unittest)\b")

# Env names that would restore the retired leg if a later change reintroduced
# one as a live knob. Their appearance inside the retained selector script is
# expected and is not a bypass -- that script is manual-only. Their appearance
# in the pre-commit config would be.
_RESTORE_KNOB_RE = re.compile(r"\b(PREPUSH_[A-Z0-9_]+|ENABLE_SMART_TESTS)\b")

# Every pytest marker expression ci.yml is allowed to select. The unit test job
# runs the first; the integration guard runs the second. A third entry here is
# a deliberate decision about what pull-request CI stops running, which is
# exactly the decision that must not be made silently now that no local leg
# runs tests behind it.
_ALLOWED_CI_MARKER_EXPRESSIONS = frozenset({"not kafka", "integration and not kafka"})

_CI_MARKER_RE = re.compile(r'-m "([^"]+)"')


def _load_config() -> dict[str, Any]:
    raw = yaml.safe_load(PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{PRECOMMIT_CONFIG} did not parse to a mapping"
    return raw


def _pre_push_hooks() -> list[dict[str, Any]]:
    """Hooks that DECLARE the pre-push stage in this config, in file order."""
    hooks: list[dict[str, Any]] = []
    for repo in _load_config().get("repos") or []:
        for hook in repo.get("hooks") or []:
            if PRE_PUSH_STAGE in (hook.get("stages") or []):
                hooks.append(hook)
    return hooks


def _hooks_reaching_pre_push() -> list[dict[str, Any]]:
    """Hooks that can actually execute on `git push`, which is a wider set.

    This config sets ``default_stages: [pre-commit]``, so a hook with no
    ``stages`` key looks pinned away from pre-push -- but ``default_stages``
    yields to a stage declaration in an upstream hook's own manifest, which
    this config cannot see and this module cannot read for a remote repo.

    So the property is asserted against every hook that is not positively
    pinned to some other stage. That over-approximates rather than
    under-approximates: a hook wrongly included merely has to not run tests,
    while a hook wrongly excluded is a test leg this gate would miss.
    """
    hooks: list[dict[str, Any]] = []
    for repo in _load_config().get("repos") or []:
        for hook in repo.get("hooks") or []:
            stages = hook.get("stages")
            if not stages or PRE_PUSH_STAGE in stages:
                hooks.append(hook)
    return hooks


def _strip_comments(text: str, marker: str = "#") -> str:
    """Drop whole-line comments so prose about a token is not read as the token.

    Root rule 15 in the other direction: a comment naming a retired knob is
    documentation, and a gate that fires on documentation about itself is a
    failure mode this fleet has already paid for three times.
    """
    kept = [line for line in text.splitlines() if not line.lstrip().startswith(marker)]
    return "\n".join(kept)


def _strip_python_prose(source: str) -> str:
    """Remove comments and docstrings from Python source, keeping other strings.

    Docstrings are the reason this exists: a validator that shells out to a
    shell script may still document, in its own docstring, a pytest command a
    reader could run by hand. Scanning raw text reads that sentence as an
    invocation and fails the retirement check on a hook that runs no tests.

    Only docstrings and comments go. Ordinary string literals stay, because
    ``subprocess.run(["pytest", ...])`` is a real invocation that lives
    entirely inside one -- dropping every string token would make this scan
    fail open on the most direct way to run a suite from Python. The positive
    control below proves both halves.

    A file that will not parse is returned with comments stripped only, so a
    syntax error degrades to the stricter reading rather than to a silent pass.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return _strip_comments(source)

    prose_lines: set[int] = set()
    for node in ast.walk(tree):
        # A bare string expression statement is a docstring, or a block comment
        # written as one. Either way it does not execute.
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.end_lineno is not None
        ):
            prose_lines.update(range(node.lineno, node.end_lineno + 1))

    kept = [
        line
        for number, line in enumerate(source.splitlines(), start=1)
        if number not in prose_lines
    ]
    return _strip_comments("\n".join(kept))


def _executable_text(path: Path) -> str:
    """A file's content with its non-executing prose removed."""
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:  # pragma: no cover - binary entry
        return ""
    if path.suffix == ".py":
        return _strip_python_prose(source)
    return _strip_comments(source)


def _command_surface(hook: dict[str, Any]) -> str:
    """The hook's entry, plus the executable body of any in-repo script it names.

    Resolving one level past the entry is the point. The plan's own first-round
    inventory missed a test invocation precisely because it searched pre-commit
    configurations for the name of a test runner, and the hook it missed named
    a script whose imported module launched the runner.
    """
    entry = str(hook.get("entry", ""))
    surface = [entry]
    for token in entry.split():
        candidate = REPO_ROOT / token
        if candidate.is_file():
            surface.append(_executable_text(candidate))
    return "\n".join(surface)


def test_no_pre_push_hook_invokes_a_test_runner() -> None:
    """The retired leg stays retired, stated as a property of the stage."""
    offenders: list[str] = []
    for hook in _hooks_reaching_pre_push():
        surface = _strip_comments(_command_surface(hook))
        match = _TEST_RUNNER_RE.search(surface)
        if match is not None:
            offenders.append(f"{hook.get('id')!r} (matched {match.group(0)!r})")

    assert not offenders, (
        "A pre-push hook invokes a test runner, which OMN-18162 retired in this "
        f"repo: {', '.join(offenders)}. Hosted CI is the enforced merge gate and "
        "the test surface. Do not re-add a test-running hook at the pre-push "
        "stage without a ruling that supersedes the operator ruling recorded in "
        ".pre-commit-config.yaml; if you have one, change this test in the same "
        "commit and cite the ruling here."
    )


def test_scan_detects_a_real_test_invocation(tmp_path: Path) -> None:
    """Positive control for the zero above (root rule 16).

    An empty offender list is only evidence when the same scan is known to
    return rows against an input that should produce one. Without this, a
    prose-stripper that swallowed the whole file, or a regex that matched
    nothing, would read exactly like a clean retirement.
    """
    shell_script = tmp_path / "runs_tests.sh"
    shell_script.write_text("#!/usr/bin/env bash\nuv run pytest tests/unit/\n")
    assert _TEST_RUNNER_RE.search(_executable_text(shell_script)) is not None, (
        "The scan failed to see a plain shell pytest invocation, so a zero from "
        "it is not evidence of a retired leg."
    )

    py_script = tmp_path / "runs_tests.py"
    py_script.write_text(
        '"""A docstring that names no runner."""\n'
        "import subprocess\n"
        'subprocess.run(["pytest", "tests/unit/"], check=True)\n'
    )
    assert _TEST_RUNNER_RE.search(_executable_text(py_script)) is not None, (
        "The scan failed to see a Python pytest invocation outside a docstring, "
        "so stripping is removing executable code and the zero is false."
    )

    prose_only = tmp_path / "documents_a_runner.py"
    prose_only.write_text('"""For a full local pass, run: pytest tests/ci/."""\n')
    assert _TEST_RUNNER_RE.search(_executable_text(prose_only)) is None, (
        "The scan read a docstring as an invocation, which is the false "
        "positive the stripping exists to remove."
    )


def test_the_retired_hook_is_unwired_at_every_stage() -> None:
    """The specific retired hook id appears at no stage in the config.

    Narrower than the property above and worth keeping separate: this one names
    what was removed, so a failure reads as "the retirement was reverted"
    rather than as a generic policy breach.
    """
    all_ids = [
        hook.get("id")
        for repo in (_load_config().get("repos") or [])
        for hook in (repo.get("hooks") or [])
    ]
    assert RETIRED_HOOK_ID not in all_ids, (
        f"The {RETIRED_HOOK_ID!r} hook is wired again. OMN-18162 retired the "
        "pre-push test leg in this repo; see the retirement block in "
        ".pre-commit-config.yaml for the plan of record and the ruling."
    )


def test_only_the_type_check_remains_at_pre_push() -> None:
    """The surviving pre-push hooks, by id and in order.

    The retirement removes exactly one hook. A change that also drops or
    reorders the type check is a different change and does not ride along on
    this one.
    """
    actual = tuple(str(hook.get("id")) for hook in _pre_push_hooks())
    assert actual == EXPECTED_PRE_PUSH_HOOK_IDS, (
        "The pre-push stage changed beyond the OMN-18162 retirement.\n"
        f"  expected: {EXPECTED_PRE_PUSH_HOOK_IDS}\n"
        f"  actual:   {actual}\n"
        "Pre-push is a type check only. Adding a non-test hook here is allowed, "
        "but update this tuple in the same commit so the stage's contents stay "
        "asserted rather than assumed."
    )


def test_selector_script_is_retained_and_marked_manual_only() -> None:
    """The script survives the retirement and says so in its header.

    The remote lab leg is launched and slot-gated under this file's name, and
    five modules under ``tests/scripts/`` pin its content, so deleting it would
    break surfaces this change never meant to touch.
    """
    assert SELECTOR_SCRIPT.is_file(), (
        f"{SELECTOR_SCRIPT.relative_to(REPO_ROOT)} was deleted. OMN-18162 "
        "unwires it from pre-push and keeps it as a manually-invocable library."
    )

    header = "\n".join(SELECTOR_SCRIPT.read_text(encoding="utf-8").splitlines()[:60])
    assert "MANUAL INVOCATION ONLY" in header, (
        "The selector script's header must state that it is manual-invocation "
        "only, so a reader who finds it does not assume it still runs on push."
    )
    assert "OMN-18162" in header, (
        "The selector script's header must cite OMN-18162, so the reason it is "
        "unwired is resolvable from the file itself."
    )


def test_retirement_introduced_no_bypass_knob() -> None:
    """No env knob in the pre-commit config restores the retired leg.

    Root rule 10: an ambient environment variable is inherited by every
    descendant process, is bound to no repo or commit, never expires and leaves
    no receipt. The retirement must not ship one, and does not need one.
    """
    executable_config = _strip_comments(PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    found = sorted(set(_RESTORE_KNOB_RE.findall(executable_config)))
    assert not found, (
        "The pre-commit config names pre-push test-leg env knobs outside a "
        f"comment: {found}. OMN-18162 retires the leg outright; a knob that "
        "turns it back on is the bypass surface the retirement removes."
    )


def test_the_retired_filter_was_a_subset_of_what_ci_selects() -> None:
    """The premise that made this retirement free of coverage loss stays true.

    Measured on 2026-09-11 by collecting both selections: 20,079 tests under
    the pre-push filter and 21,301 under CI's, with the pre-push set a strict
    subset. Nothing lost its only execution path, and the whole 1,222-test
    difference is ``tests/integration``, which CI runs and pre-push never did.

    That held because the two marker filters were identical -- both ``not
    kafka`` -- and pre-push additionally ignored the integration tree. Now that
    no local leg runs tests, narrowing CI's filter is the way a gap opens, so
    the filters are pinned here rather than re-measured by hand.
    """
    selector = SELECTOR_SCRIPT.read_text(encoding="utf-8")
    assert 'LOCAL_MARKER_FILTER="not kafka"' in selector, (
        "The retained selector no longer declares the marker filter this "
        "module's subset finding was measured against. Re-measure the two "
        "selections before changing this assertion."
    )

    ci_markers = set(_CI_MARKER_RE.findall(CI_WORKFLOW.read_text(encoding="utf-8")))
    unexpected = sorted(ci_markers - _ALLOWED_CI_MARKER_EXPRESSIONS)
    assert not unexpected, (
        "ci.yml selects a pytest marker expression this module has not "
        f"accounted for: {unexpected}. Pull-request CI is now the only surface "
        "that runs these tests, so a new deselection removes an execution path "
        "outright. Give the deselected tests a positively-selecting job, then "
        "add the expression here in the same commit."
    )


def test_the_retirement_block_does_not_spell_the_legacy_grep_literals() -> None:
    """A stale bootstrap must not read this documentation as wiring.

    This repo's pre-OMN-17851 pre-push bootstrap, which is still installed on
    machines that have not re-run the installer, greps
    ``.pre-commit-config.yaml`` for the removed hook's id and for its ``entry:``
    line, and treats a match as proof that the governed test gate is present.
    Those are substring matches, so writing either literal into the retirement
    comment would satisfy the check off a comment and turn a fail-closed
    bootstrap into a fail-open one -- permanently, and invisibly.

    So the block describes the removed hook in words. A stale bootstrap now
    refuses the push, which is the correct direction to fail in, and the config
    block and CLAUDE.md both name the remedy.
    """
    config_text = PRECOMMIT_CONFIG.read_text(encoding="utf-8")
    for literal in (
        f"id: {RETIRED_HOOK_ID}",
        "entry: bash scripts/hooks/prepush_smart_tests.sh",
    ):
        assert literal not in config_text, (
            f"The pre-commit config contains {literal!r}. The superseded "
            "pre-push bootstrap greps this file for that exact string and "
            "would report a vacuous pass off a comment. Describe the retired "
            "hook in words instead."
        )


def test_the_tracked_bootstrap_installer_has_no_hook_authority_grep() -> None:
    """The installer's generated bootstrap does not gate on a hook id either.

    The superseded bootstrap decided whether to run by grepping the pre-commit
    config for one hook's id and entry. That check is why a repo can never
    retire a hook without also breaking every stale install, and it is a
    substring match over a file that contains its own documentation. The
    current installer does not carry it, and this asserts it does not come
    back.
    """
    installer = _strip_python_prose(BOOTSTRAP_INSTALLER.read_text(encoding="utf-8"))
    assert RETIRED_HOOK_ID not in installer, (
        f"{BOOTSTRAP_INSTALLER.relative_to(REPO_ROOT)} names the retired hook "
        "id in executable code. A bootstrap that refuses to run unless the "
        "pre-commit config mentions a particular hook id cannot distinguish "
        "wiring from a comment about wiring."
    )
