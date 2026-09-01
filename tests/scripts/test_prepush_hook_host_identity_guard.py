# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Regression guard for the pre-push hook's `.200`-default host-identity
guard (OMN-15059).

Root cause this prevents regressing: root CLAUDE.md documents that the heavy
(full-suite, fail-closed escalation) branch of the pre-push hook defaults to
running on the `.200` execution host, not the local Mac -- but a rule stated
only in a doc/prompt has zero enforcement force without a call-site mechanism
(memory `feedback_a_rule_is_not_a_mechanism`). Evidence this was load-bearing:
a 2026-07-24 session drove the local Mac to load ~55 with 93% swap running
this exact escalation for 115+ minutes before `.200` was invoked as a rescue
rather than having been the execution target from the start.

Three assertion classes:

1. Static wiring -- `guard_full_suite_host` is defined and is the first
   statement inside EVERY `IS_FULL` (full-suite) branch, so a future edit
   cannot silently drop the call site while leaving the branch intact.
2. Behavioral -- actually invoking the hook with the full-suite escalation
   forced (`PREPUSH_FULL_SUITE=1`) and a guaranteed-non-matching
   `PREPUSH_200_HOSTNAME` override exits non-zero WITHOUT ever reaching the
   real pytest invocation. The override makes this host-independent: it must
   hold true no matter which host runs the test suite (including `.200`
   itself), so the test does not rely on the ambient hostname.
3. Heavyweight-SELECTION (OMN-15408) -- the guard must key on the work the
   selector actually picked, not on the `is_full_suite` flag. The selector
   routinely emits `is_full_suite=False` with `selected_paths=["tests/"]`, and
   before OMN-15408 those runs bypassed the guard outright: 13,898 tests /
   506s in omnimarket and 2,429 tests / 245s in omnibase_infra, executed on
   `omnibook` through real `git push` runs on 2026-07-29 with the guard never
   invoked -- while the identical selected work forced via
   `PREPUSH_FULL_SUITE=1` WAS refused. Assertion classes 1 and 2 above were
   green that entire time, because both only ever drove the flag-true path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from tests.scripts._prepush_lab_isolation import network_free_lab_env

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "prepush_smart_tests.sh"

_GUARANTEED_NON_MATCHING_HOSTNAME = "definitely-not-the-200-host-omn15059"


def _designated_row_labels() -> tuple[str, ...]:
    """Every label in the COMMITTED host table, read from the table itself.

    Read rather than hardcoded so that adding a row to
    ``scripts/hooks/prepush_hosts.tsv`` cannot silently leave that row
    designated inside a refusal proof. A hardcoded list is the exact defect
    OMN-17435 fixed one layer up: the guard used to know two hostnames, and a
    third host was invisible to it by construction.
    """
    table = REPO_ROOT / "scripts" / "hooks" / "prepush_hosts.tsv"
    labels = []
    for line in table.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0]
        if not line.strip():
            continue
        labels.append(line.split("\t")[0])
    assert labels, f"expected at least one data row in {table}"
    return tuple(labels)


def _capacity_row_labels() -> tuple[str, ...]:
    """Labels of rows that are placement candidates (role=capacity)."""
    table = REPO_ROOT / "scripts" / "hooks" / "prepush_hosts.tsv"
    labels = []
    for line in table.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0]
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) > 1 and fields[1] == "capacity":
            labels.append(fields[0])
    assert labels, f"expected at least one capacity row in {table}"
    return tuple(labels)


def _skip_if_the_shared_host_slot_is_held(
    result: subprocess.CompletedProcess[str],
) -> None:
    """Skip when a REAL heavy push holds this host's exclusive slot.

    The local heavy path takes the same `<workroot>/LOCK` a remote host would
    (OMN-16174 -- before that lock existed, five concurrent full suites once ran
    on one machine). The workroot is shared across repos and lanes, so a genuine
    push in flight legitimately makes this host unplaceable, and the guard then
    refuses exactly as designed. That is the mechanism working, not a defect --
    but it is also not what this test measures, so it is skipped rather than
    reported as a failure or, worse, asserted away.
    """
    if "slot is already held" in result.stderr:
        import pytest

        pytest.skip(
            "another heavy pre-push run holds this host's exclusive slot; "
            "the fit-host path cannot be exercised while it is held"
        )


def _force_undesignated_host(env: dict[str, str]) -> None:
    """Make the ambient host un-designated for EVERY table row (OMN-16752,
    widened by OMN-17435).

    Before OMN-17435 the guard admitted a host if `hostname -s` matched either
    of TWO identities: `.200` (``PREPUSH_200_HOSTNAME``) or the `.201`
    gate-runner container (``PREPUSH_201_GATE_RUNNER_HOSTNAME``, default
    ``gate-runner-201``), so neutralising those two names was sufficient.

    IT IS NO LONGER SUFFICIENT, and that is the whole point of this port.
    Identity now comes from ``scripts/hooks/prepush_hosts.tsv``, which
    designates `h101` (stickybeatz), `h105` (omnibook) and the `.201` HOST
    itself (`h201`, omninode-pc) in addition to those two. On any of those
    machines a two-name override leaves the row live, the guard correctly
    ALLOWS, and every refusal proof below inverts. So this neutralises every
    row via the picker's own per-row seam,
    ``PREPUSH_HOST_OVERRIDE_<LABEL uppercased>``, enumerated FROM the table.

    That is not hypothetical, and the cost was not a cosmetic red. The `.201`
    gate-runner is the host the OMN-16295 capacity guard routes to when `.200`
    is over threshold — i.e. the one host where the full suite is MOST likely
    to run — and its container sets ``hostname: gate-runner-201`` precisely so
    the guard recognises it. Running this file there on 2026-08-27 turned
    ``test_guard_refuses_full_suite_escalation_on_non_200_host`` (which drives
    the REAL hook, with no stubbed pytest) from a refusal proof into a live
    RECURSIVE FULL-SUITE FORK BOMB: the guard allowed, the hook ran the whole
    suite, that suite re-reached this test, popped the recursion sentinel
    (documented below as deliberate first-entry behavior) and re-entered —
    measured four nested full suites deep, one new level per minute, on the
    shared `.201` host that also carries the runner fleet and every runtime
    lane. With three more designated hosts in the table, the same fork bomb is
    now reachable on three more machines.

    So this override is a correctness AND a containment requirement. Neutralise
    every row; do not narrow this to a subset.
    """
    # The two legacy aliases the picker still honors, kept so a reader sees the
    # continuity with the pre-table behavior.
    env["PREPUSH_200_HOSTNAME"] = _GUARANTEED_NON_MATCHING_HOSTNAME
    env["PREPUSH_201_GATE_RUNNER_HOSTNAME"] = _GUARANTEED_NON_MATCHING_HOSTNAME
    # And every row, including any added after this was written.
    for label in _designated_row_labels():
        var = "PREPUSH_HOST_OVERRIDE_" + "".join(
            c if c.isalnum() else "_" for c in label.upper()
        )
        env[var] = _GUARANTEED_NON_MATCHING_HOSTNAME
    # OMN-16991/OMN-17435: hold the lab-dispatch leg network-free. Without it a
    # refusal proof ships a real bundle to a lab host and starts the whole
    # suite there. See tests/scripts/_prepush_lab_isolation.
    env.update(network_free_lab_env())


_FULL_SUITE_BRANCH_RE = re.compile(
    r'if \[ "\$IS_FULL" = "True" \].*?\n(.*?\n)(?=elif|else|fi)',
    re.DOTALL,
)


def test_hook_script_exists() -> None:
    assert HOOK_SCRIPT.is_file(), f"expected pre-push hook at {HOOK_SCRIPT}"


def test_guard_function_is_defined() -> None:
    script_text = HOOK_SCRIPT.read_text(encoding="utf-8")
    assert "guard_full_suite_host()" in script_text, (
        "expected a guard_full_suite_host() function guarding the heavy "
        f"full-suite escalation in {HOOK_SCRIPT}"
    )


def test_guard_is_called_before_every_full_suite_pytest_invocation() -> None:
    """Every `if [ "$IS_FULL" = ... ]; then` branch must call
    guard_full_suite_host as its first statement, so the heavy escalation can
    never run without the host check firing first."""
    script_text = HOOK_SCRIPT.read_text(encoding="utf-8")
    full_suite_branches = _FULL_SUITE_BRANCH_RE.findall(script_text)
    assert full_suite_branches, "expected at least one IS_FULL branch in the hook"
    for branch_body in full_suite_branches:
        first_stmt = next(
            (line.strip() for line in branch_body.splitlines() if line.strip()),
            "",
        )
        assert first_stmt == "guard_full_suite_host", (
            "expected guard_full_suite_host to be the first statement in the "
            f"full-suite branch, found {first_stmt!r} instead"
        )


def test_guard_fails_closed_when_hostname_cannot_be_determined() -> None:
    """OMN-16489 defect 3 inverted this pin: the empty-hostname branch used to
    WARN and return 0 (fail-open), letting the heavy escalation proceed on a
    host that could not be identified. It now refuses with remediation. The
    behavioral proof lives in test_prepush_hook_recursion_and_env_guard.py."""
    script_text = HOOK_SCRIPT.read_text(encoding="utf-8")
    assert 'if [ -z "$host" ]; then' in script_text, (
        "expected the guard to check for an unresolvable hostname"
    )
    assert "could not determine the local hostname" in script_text, (
        "expected the empty-hostname branch to die with a remediation message "
        "(fail-closed, OMN-16489)"
    )
    assert "proceeding locally (fail-open" not in script_text, (
        "the empty-hostname branch must not fail open (OMN-16489)"
    )


def test_the_escape_hatch_is_a_receipted_grant_not_an_env_var() -> None:
    """OMN-16480, ported to this repo by OMN-17435.

    This gate's escape hatch used to BE ``PREPUSH_ALLOW_LOCAL_FULL_SUITE=1``.
    An environment variable is inherited by every descendant process, bound to
    no repo/commit/run, never expires and leaves no receipt -- so one leaked
    value disarmed the gate for a whole process tree and recursively spawned
    another full suite. The variable is now a HARD REFUSAL in both directions,
    and the supported path is a single-use, repo+HEAD-scoped, TTL-bounded,
    receipted grant.

    Both halves are asserted, because either alone would be a different change:
    the rejection must be CALLED (defining a function is not enforcing it), and
    a replacement must EXIST (removing an escape hatch with nothing in its place
    is not this change).
    """
    script_text = HOOK_SCRIPT.read_text(encoding="utf-8")
    assert "reject_inherited_env_overrides" in script_text
    assert any(
        line.strip() == "reject_inherited_env_overrides"
        for line in script_text.splitlines()
    ), "the entry rejection is defined but never called"
    assert "consume_override_grant" in script_text, (
        "the env-var hatch was removed with no receipted replacement"
    )
    assert "prepush_override_grant.py" in script_text, (
        "expected the refusal to name the grant-minting command"
    )
    assert "DEGRADED-HOST OVERRIDE" in script_text, (
        "expected the override to print a loud, visible warning naming the "
        "degraded evidence -- a silent bypass reproduces the incident this "
        "guard exists to prevent"
    )


def test_guard_refuses_full_suite_escalation_on_non_200_host() -> None:
    """Behavioral proof: force the full-suite escalation and a
    guaranteed-non-matching host; the hook must exit non-zero and must NEVER
    reach the actual pytest invocation.

    ``PREPUSH_ALLOW_LOCAL_FULL_SUITE`` must be popped from the copied
    environment before invoking the hook: it is the documented
    "DEGRADED-HOST OVERRIDE" escape hatch (see the assertions above), and an
    ambient value inherited from whoever is running THIS test suite (e.g. a
    developer who set it to push past a loaded-host refusal moments earlier)
    would route the hook through the override branch instead of the refusal
    this test asserts -- a false negative on the exact guard this test exists
    to prove. Mirrors the leaky-var pop already applied in ``_run_hook``
    below for the identical reason.
    """
    env = dict(os.environ)
    env.pop("PREPUSH_ALLOW_LOCAL_FULL_SUITE", None)
    # OMN-16489: this test deliberately exercises FIRST-entry behavior, so the
    # recursion sentinel an outer hook run exports must not leak in.
    env.pop("ONEX_PREPUSH_HOOK_ACTIVE", None)
    env["PREPUSH_FULL_SUITE"] = "1"
    _force_undesignated_host(env)
    # OMN-16428: this test proves the guard REFUSES without an override in
    # effect. If the ambient shell that invoked pytest itself had
    # PREPUSH_ALLOW_LOCAL_FULL_SUITE=1 set (the sanctioned degraded-capacity
    # escape hatch a few lines up in this same guard, used for real during a
    # sustained .200/.201 fleet-load incident), `dict(os.environ)` inherits
    # it into the subprocess under test and the guard legitimately takes the
    # override branch instead of refusing -- a false failure of this test,
    # not a guard defect. Pop it so this specific negative-path proof stays
    # isolated from whatever override state the outer pytest process was
    # invoked under.
    env.pop("PREPUSH_ALLOW_LOCAL_FULL_SUITE", None)
    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode != 0, (
        "expected the host guard to refuse the full-suite escalation on a "
        f"non-.200 host; got exit {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "not the designated .200 build host" in result.stderr, (
        f"expected the refusal message in stderr, got: {result.stderr!r}"
    )
    assert "collected" not in result.stdout, (
        "the guard must refuse BEFORE pytest ever collects tests -- found a "
        f"pytest collection banner in stdout: {result.stdout!r}"
    )


# =============================================================================
# OMN-15408: the guard must key on the SELECTED WORK, not the is_full_suite flag
# =============================================================================
# The OMN-15059 guard above was called ONLY from inside the `IS_FULL` branch, so
# it fired on the selector's `is_full_suite` FLAG. The selector routinely emits
# `is_full_suite=False` with `selected_paths=["tests/"]` -- the whole suite
# arriving as an "impacted subset" -- and those runs bypassed the guard
# entirely. Measured on host `omnibook` through real `git push` runs on
# 2026-07-29: omnimarket ran 13,898 tests in 506s and omnibase_infra 2,429 tests
# in 245s, locally, with the guard never invoked; the SAME selected work forced
# via `PREPUSH_FULL_SUITE=1` was refused. Identical cost, opposite outcome,
# decided by a flag. The tests below drive the flag-FALSE path, which is the one
# that reaches production behavior -- the pre-existing tests in this file only
# ever exercised the flag-TRUE path, and were green while the hole was open.
#
# `test_guard_refuses_whole_suite_equivalent_selection_when_flag_is_false` is
# RED against the pre-fix hook (it proceeds to pytest, exit 0) and GREEN after.
# `test_guard_allows_a_genuinely_narrow_selection` is the anti-overreach pin:
# the fix must not brick every push from this Mac.

_FULL_SUITE_TARGET = "tests/"
# The literal production shape from the OMN-15408 evidence table.
_WHOLE_SUITE_SELECTION = "tests/"
# A real, genuinely-narrow subdirectory of this repo's suite.
_NARROW_SELECTION = "tests/scripts/"

_PREDICATE_RE = re.compile(
    r"^selection_is_whole_suite\(\) \{.*?^\}",
    re.DOTALL | re.MULTILINE,
)
_WHOLE_SUITE_GUARD_CALL_RE = re.compile(
    r"if selection_is_whole_suite .*?\n(.*?)\n\s*fi",
    re.DOTALL,
)


def _extract_predicate_source() -> str:
    """Return the literal `selection_is_whole_suite` bash function from the hook.

    Extract-and-execute (the pattern already used for the hook's other pure
    shell helpers) so these assertions run THE function that ships, never a
    Python re-implementation of it -- a re-implementation would pass happily
    while the shipped predicate was broken.
    """
    match = _PREDICATE_RE.search(HOOK_SCRIPT.read_text(encoding="utf-8"))
    assert match is not None, (
        "expected a selection_is_whole_suite() function in "
        f"{HOOK_SCRIPT} -- the OMN-15408 heavyweight-selection predicate"
    )
    return match.group(0)


def _predicate_says_whole_suite(target: str, *paths: str) -> bool:
    """Execute the real bash predicate; True == 'this selection is heavyweight'."""
    script = f'{_extract_predicate_source()}\nselection_is_whole_suite "$@"\n'
    completed = subprocess.run(
        ["bash", "-c", script, "selection_is_whole_suite", target, *paths],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode in (0, 1), (
        "predicate exited abnormally "
        f"({completed.returncode}): stderr={completed.stderr!r}"
    )
    return completed.returncode == 0


def _run_hook_with_stubbed_selection(
    tmp_path: Path,
    *,
    is_full_suite: bool,
    selected_paths: list[str],
) -> subprocess.CompletedProcess[str]:
    """Run the REAL hook end-to-end with a stubbed selector + stubbed pytest.

    A shim `uv` earlier on PATH answers the selector invocation with a chosen
    selection JSON and turns the pytest invocation into an observable sentinel,
    so the test can assert both `did the guard refuse` and `did execution ever
    reach pytest` against the actual script rather than a surrogate. Everything
    else (`uv run python - <heredoc>` for JSON parsing) is delegated to the
    real interpreter.

    `PREPUSH_BASE_REF=HEAD` keeps the pre-selector preamble deterministic and
    offline: it always resolves, `merge-base HEAD HEAD` is HEAD, and the diff is
    empty -- the stub supplies the selection regardless.
    """
    selection_file = tmp_path / "selection.json"
    selection_file.write_text(
        json.dumps(
            {
                "is_full_suite": is_full_suite,
                "full_suite_reason": None,
                "selected_paths": selected_paths,
            }
        ),
        encoding="utf-8",
    )

    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(parents=True, exist_ok=True)
    uv_stub = stub_bin / "uv"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        'args="$*"\n'
        'case "$args" in\n'
        "  *detect_test_paths*)\n"
        '    cat "$PREPUSH_TEST_SELECTION_JSON"\n'
        "    exit 0\n"
        "    ;;\n"
        "  *pytest*)\n"
        '    echo "STUB-PYTEST-INVOKED $args"\n'
        "    exit 0\n"
        "    ;;\n"
        "esac\n"
        "shift\n"
        'if [ "$1" = "python" ]; then\n'
        "  shift\n"
        '  exec python3 "$@"\n'
        "fi\n"
        'exec "$@"\n',
        encoding="utf-8",
    )
    uv_stub.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    env["PREPUSH_TEST_SELECTION_JSON"] = str(selection_file)
    env["PREPUSH_BASE_REF"] = "HEAD"
    _force_undesignated_host(env)
    for leaky in (
        "PREPUSH_FULL_SUITE",
        "PREPUSH_ALLOW_LOCAL_FULL_SUITE",
        "ENABLE_SMART_TESTS",
        "PREPUSH_ADJACENCY",
        "PREPUSH_PYTEST_ARGS",
        # OMN-16489: this harness exercises FIRST-entry behavior, so the
        # recursion sentinel an outer hook run exports must not leak in.
        "ONEX_PREPUSH_HOOK_ACTIVE",
    ):
        env.pop(leaky, None)

    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_heavyweight_selection_predicate_is_defined() -> None:
    assert "selection_is_whole_suite()" in HOOK_SCRIPT.read_text(encoding="utf-8"), (
        "expected a selection_is_whole_suite() predicate so the guard can key "
        "on the selected work rather than the is_full_suite flag (OMN-15408)"
    )


def test_full_suite_target_is_single_sourced() -> None:
    """The predicate and the escalation must read the SAME target.

    If the guard hard-coded its own notion of 'the whole suite' it would drift
    from whatever the escalation actually runs -- a second cost model, which is
    the thing this fix explicitly avoids.
    """
    script_text = HOOK_SCRIPT.read_text(encoding="utf-8")
    assert f'FULL_SUITE_TARGET="{_FULL_SUITE_TARGET}"' in script_text, (
        f"expected FULL_SUITE_TARGET to be set to {_FULL_SUITE_TARGET!r} in "
        f"{HOOK_SCRIPT}"
    )
    assert 'uv run pytest "${FULL_SUITE_TARGET}"' in script_text, (
        "expected the fail-closed escalation to run ${FULL_SUITE_TARGET} "
        "itself, so the guard predicate cannot drift from the run it guards"
    )
    assert 'selection_is_whole_suite "$FULL_SUITE_TARGET"' in script_text, (
        "expected the predicate to be evaluated against the SAME "
        "FULL_SUITE_TARGET the escalation runs"
    )


def test_guard_is_called_when_the_selection_is_whole_suite_equivalent() -> None:
    """Static wiring: the impacted-subset branch must consult the guard.

    Pairs with the pre-existing IS_FULL-branch assertion above; together they
    pin BOTH call sites, so a future edit cannot silently drop either one.
    """
    script_text = HOOK_SCRIPT.read_text(encoding="utf-8")
    guarded_blocks = _WHOLE_SUITE_GUARD_CALL_RE.findall(script_text)
    assert guarded_blocks, (
        "expected an `if selection_is_whole_suite ...` block in the "
        "impacted-subset branch (OMN-15408)"
    )
    assert any("guard_full_suite_host" in block for block in guarded_blocks), (
        "expected guard_full_suite_host to be called when the selection is "
        f"whole-suite-equivalent; found blocks: {guarded_blocks!r}"
    )


def test_predicate_flags_a_whole_suite_selection() -> None:
    """A selection covering the entire escalation target is heavyweight."""
    assert _predicate_says_whole_suite(_FULL_SUITE_TARGET, _WHOLE_SUITE_SELECTION)
    assert _predicate_says_whole_suite(_FULL_SUITE_TARGET, _FULL_SUITE_TARGET)
    assert _predicate_says_whole_suite(
        _FULL_SUITE_TARGET, _FULL_SUITE_TARGET.rstrip("/")
    ), "a trailing-slash-less path must normalize to the same target"
    assert _predicate_says_whole_suite(
        _FULL_SUITE_TARGET, _NARROW_SELECTION, _WHOLE_SUITE_SELECTION
    ), "one whole-suite path anywhere in the selection makes it heavyweight"


def test_predicate_allows_a_genuinely_narrow_selection() -> None:
    """Real narrowing stays runnable -- the guard is not a blanket push block."""
    assert not _predicate_says_whole_suite(_FULL_SUITE_TARGET, _NARROW_SELECTION)
    assert not _predicate_says_whole_suite(
        _FULL_SUITE_TARGET, f"{_NARROW_SELECTION}test_something.py"
    )
    assert not _predicate_says_whole_suite(_FULL_SUITE_TARGET)


def test_guard_refuses_whole_suite_equivalent_selection_when_flag_is_false(
    tmp_path: Path,
) -> None:
    """THE OMN-15408 REGRESSION.

    `is_full_suite=False` + `selected_paths=["tests/"]` on a non-`.200` host is
    the exact shape that ran 13,898 tests locally with the guard never invoked.
    RED against the pre-fix hook (it reached pytest and exited 0); GREEN after.
    """
    result = _run_hook_with_stubbed_selection(
        tmp_path,
        is_full_suite=False,
        selected_paths=[_WHOLE_SUITE_SELECTION],
    )
    assert result.returncode != 0, (
        "expected the host guard to refuse a whole-suite-equivalent selection "
        "even though is_full_suite=False; got exit "
        f"{result.returncode}. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "not the designated .200 build host" in result.stderr, (
        f"expected the refusal message in stderr, got: {result.stderr!r}"
    )
    assert "STUB-PYTEST-INVOKED" not in result.stdout, (
        "the guard must refuse BEFORE pytest is invoked -- found a pytest "
        f"invocation in stdout: {result.stdout!r}"
    )


def test_guard_allows_a_genuinely_narrow_selection_on_a_local_host(
    tmp_path: Path,
) -> None:
    """Anti-overreach pin.

    A real narrow selection must still run locally on a non-`.200` host. If
    this ever fails, the fix has become a blanket push block and will be
    disabled within a week -- which is worse than no guard at all.
    """
    result = _run_hook_with_stubbed_selection(
        tmp_path,
        is_full_suite=False,
        selected_paths=[_NARROW_SELECTION],
    )
    assert result.returncode == 0, (
        "expected a genuinely narrow selection to be allowed on a non-.200 "
        f"host; got exit {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "not the designated .200 build host" not in result.stderr, (
        f"the guard must NOT refuse a proper narrowing; stderr: {result.stderr!r}"
    )
    assert "STUB-PYTEST-INVOKED" in result.stdout, (
        "expected the narrow selection to actually reach pytest; stdout: "
        f"{result.stdout!r}"
    )
    assert _NARROW_SELECTION in result.stdout, (
        "expected the narrow selection's own path to be handed to pytest; "
        f"stdout: {result.stdout!r}"
    )


# =============================================================================
# OMN-16295 live-load selection: the helpers moved under the picker (OMN-17435)
# =============================================================================
# This file used to extract `host_load_ratio` / `host_is_fit` /
# `_prepush_timeout_cmd` out of the hook with a regex and execute them in a
# bare `bash -c`, which worked while they were self-contained.
#
# They are not self-contained any more. `host_load_ratio` now calls
# `reap_spin_loop_orphans` (OMN-16995: reap leaked spin-loop orphans BEFORE
# measuring, because 19 of them once put `.200` at 1.64x-core and refused every
# heavy escalation in the lab), and that function lives in
# `scripts/hooks/prepush_dispatch.sh`. Extracting the helpers alone therefore
# produced `reap_spin_loop_orphans: command not found` and rc=2 -- a harness
# failure that says nothing about the helpers.
#
# The right fix is NOT to extract more text until it links. It is to test the
# helpers where they actually run: `tests/scripts/test_prepush_host_table.py`
# sources the REAL library and exercises both fitness dimensions against it --
# `test_host_is_fit_accepts_a_host_that_proves_both_dimensions`,
# `test_host_is_fit_refuses_a_host_that_is_idle_but_out_of_memory`,
# `test_host_is_fit_reports_unreadable_memory_as_could_not_check`, and
# `test_a_memory_starved_host_is_unfit_even_at_zero_load`. This pin makes that
# coupling explicit so the coverage cannot be dropped by deleting a file and
# assuming the other one still has it.


def test_the_load_helpers_are_covered_by_the_host_table_suite() -> None:
    """The helpers this file used to extract are exercised elsewhere -- pinned
    here so their coverage cannot vanish silently."""
    sibling = REPO_ROOT / "tests" / "scripts" / "test_prepush_host_table.py"
    assert sibling.is_file(), (
        "the load-helper coverage moved to test_prepush_host_table.py; that "
        "file is gone, so the helpers are now untested"
    )
    text = sibling.read_text(encoding="utf-8")
    for name in (
        "host_is_fit",
        "host_load_ratio",
        "PREPUSH_MIN_FREE_MEM_MB",
    ):
        assert name in text, (
            f"{name} is no longer referenced by the host-table suite -- the "
            "coverage this file gave up was not picked up there"
        )


def test_the_hook_still_defines_the_load_helpers_it_delegates_to() -> None:
    """The helpers must remain in the HOOK, not migrate into the vendored
    picker.

    `prepush_dispatch.sh` is a byte-identical copy of omnibase_infra's and
    deliberately reuses the caller's `host_load_ratio` / `host_is_fit` /
    `_prepush_timeout_cmd` rather than reimplementing them. Moving them out of
    the hook would fork the copy on the next sync.
    """
    text = HOOK_SCRIPT.read_text(encoding="utf-8")
    for fn in ("host_load_ratio()", "host_is_fit()", "_prepush_timeout_cmd()"):
        assert fn in text, f"{fn} is no longer defined in {HOOK_SCRIPT.name}"


def _run_hook_forcing_full_suite(
    *, env_extra: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the REAL hook with the full-suite escalation forced
    (`PREPUSH_FULL_SUITE=1`) and the REAL governed selector (a fast, real `uv
    run` invocation) -- but with a stub `uv` that intercepts only the eventual
    `pytest` invocation, so the (potentially minutes-long) real test run never
    happens while every decision up to and including `guard_full_suite_host`
    is exercised for real."""
    real_uv = shutil.which("uv")
    assert real_uv is not None, "expected uv on PATH to run this test"

    with tempfile.TemporaryDirectory() as tmp:
        stub_bin = Path(tmp) / "stub-bin"
        stub_bin.mkdir(parents=True, exist_ok=True)
        uv_stub = stub_bin / "uv"
        uv_stub.write_text(
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            "  *pytest*)\n"
            '    echo "STUB-PYTEST-INVOKED $*"\n'
            "    exit 0\n"
            "    ;;\n"
            "esac\n"
            f'exec "{real_uv}" "$@"\n',
            encoding="utf-8",
        )
        uv_stub.chmod(0o755)

        env = dict(os.environ)
        env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
        env["PREPUSH_FULL_SUITE"] = "1"
        env["PREPUSH_BASE_REF"] = "HEAD"
        # OMN-16489: ONEX_PREPUSH_HOOK_ACTIVE is popped for the same reason as
        # the override var -- this helper exercises FIRST-entry behavior.
        for leaky in ("PREPUSH_ALLOW_LOCAL_FULL_SUITE", "ONEX_PREPUSH_HOOK_ACTIVE"):
            env.pop(leaky, None)
        # OMN-16991/OMN-17435: hold the lab-dispatch leg network-free. Two
        # distinct hazards, both live before this line existed:
        #   * this harness runs the REAL hook, which can now ship a bundle to a
        #     lab host and start the whole suite there from inside a unit test;
        #   * a lab row that probes `busy`/`over` is TRANSIENT, so the guard
        #     spends the full 900s off-box wait budget -- past this harness's
        #     120s subprocess timeout, turning a behavioral proof into a
        #     TimeoutExpired that says nothing about the guard.
        # An unknown slot is structural, so every row is skipped with no ssh and
        # no wait, and the hook falls straight through to the refusal ladder
        # these tests are actually about.
        env.update(network_free_lab_env())
        env.update(env_extra)

        return subprocess.run(
            ["bash", str(HOOK_SCRIPT)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


def test_guard_refuses_known_good_host_that_is_over_the_load_threshold() -> None:
    """The core OMN-16295 behavior: identity match is no longer sufficient.

    PREPUSH_LOAD_OVERRIDE_REMOTE is set (to an arbitrary fit value) so the
    "other host" probe never makes a real network call -- this test is only
    about the SELF-host refusal, not about which remediation phrasing the
    other-host probe produces (that's covered by the two tests below)."""
    result = _run_hook_forcing_full_suite(
        env_extra={
            "PREPUSH_200_HOSTNAME": _real_short_hostname(),
            "PREPUSH_LOAD_OVERRIDE_LOCAL": "50 24",
            "PREPUSH_LOAD_OVERRIDE_REMOTE": "3 32",
        },
    )
    assert result.returncode != 0, (
        f"expected refusal on an over-threshold known-good host: {result!r}"
    )
    assert "at/over the" in result.stderr, (
        f"expected the capacity-refusal message, got stderr={result.stderr!r}"
    )
    assert "x-core threshold" in result.stderr, (
        f"expected the capacity-refusal message, got stderr={result.stderr!r}"
    )
    assert "STUB-PYTEST-INVOKED" not in result.stdout, (
        "the guard must refuse BEFORE pytest is ever invoked"
    )


def test_guard_allows_known_good_host_that_is_under_the_load_threshold() -> None:
    """A fit, designated host still reaches pytest -- the anti-overreach pin.

    TWO things changed under OMN-17435 and both are visible here.
    (1) The override is now a THREE-field reading `<load1> <nproc> <mem_mib>`:
    the two-field form means "memory could not be read", which OMN-17392 treats
    as UNFIT rather than ample, so the historical `"2 24"` would refuse for a
    reason that has nothing to do with load. (2) `h200` is
    `heavy_local=prefer_remote`, so the hook asks the lab FIRST; the lab
    isolation makes every row structurally unavailable, the bounded wait is
    skipped, and control reaches the local slot -- which is the path this test
    is about.
    """
    result = _run_hook_forcing_full_suite(
        env_extra={
            "PREPUSH_200_HOSTNAME": _real_short_hostname(),
            "PREPUSH_LOAD_OVERRIDE_LOCAL": "2 24 65536",
        },
    )
    _skip_if_the_shared_host_slot_is_held(result)
    assert result.returncode == 0, f"expected success on a fit known host: {result!r}"
    assert "STUB-PYTEST-INVOKED" in result.stdout, (
        f"expected the run to reach pytest: {result!r}"
    )


def test_guard_allows_the_201_gate_runner_identity_when_fit() -> None:
    """The .201 gate-runner is a valid execution host by identity, not just
    `.200` -- this is the routing half of OMN-16295."""
    result = _run_hook_forcing_full_suite(
        env_extra={
            "PREPUSH_201_GATE_RUNNER_HOSTNAME": _real_short_hostname(),
            "PREPUSH_200_HOSTNAME": "definitely-not-this-host",
            "PREPUSH_LOAD_OVERRIDE_LOCAL": "2 24 65536",
        },
    )
    _skip_if_the_shared_host_slot_is_held(result)
    assert result.returncode == 0, (
        f"expected success when this host matches the .201 gate-runner "
        f"identity and is fit: {result!r}"
    )
    assert "STUB-PYTEST-INVOKED" in result.stdout, result


def test_the_env_var_override_is_now_refused_at_entry_not_honored() -> None:
    """OMN-16480, ported here by OMN-17435 -- this test is INVERTED on purpose.

    It used to assert that `PREPUSH_ALLOW_LOCAL_FULL_SUITE=1` forced through
    the capacity check. That variable is no longer an arming signal in either
    direction: it is inherited by every descendant process, bound to no
    repo/commit/run, never expires and leaves no receipt, and one leaked value
    recursively spawned another full suite (~9h03m, friction F-01/F-04). Its
    presence is now a HARD REFUSAL at hook entry, BEFORE the selector runs.

    Inverting an existing assertion is the honest way to record that: deleting
    the test would leave no evidence the behavior ever changed, and leaving it
    green would require keeping the bypass.
    """
    result = _run_hook_forcing_full_suite(
        env_extra={
            "PREPUSH_200_HOSTNAME": _real_short_hostname(),
            "PREPUSH_LOAD_OVERRIDE_LOCAL": "50 24",
            "PREPUSH_ALLOW_LOCAL_FULL_SUITE": "1",
        },
    )
    assert result.returncode != 0, (
        f"a leaked PREPUSH_ALLOW_* variable must be REFUSED, not honored: {result!r}"
    )
    assert "REJECTED, never honored" in result.stderr, result.stderr
    assert "prepush_override_grant.py" in result.stderr, (
        "the refusal must name the receipted replacement, not just say no"
    )
    assert "STUB-PYTEST-INVOKED" not in result.stdout, (
        "the rejection must fire before any pytest is invoked"
    )


def test_the_refusal_reports_the_whole_probe_trail_not_one_other_host() -> None:
    """The refusal names EVERY row it probed and why each was rejected.

    Before OMN-17435 this pair of tests asserted a two-host remediation
    sentence -- "the .201 gate-runner currently HAS capacity -- route there
    instead" / "is ALSO at/over the load threshold". That phrasing could only
    ever exist because the guard knew exactly two hosts. With a table there is
    no singular "other host" to name, and a refusal that named one would hide
    the other three. The replacement is strictly more informative: a
    per-row verdict trail, which is also what tells a reader whether the lab
    was saturated or simply unreachable.
    """
    result = _run_hook_forcing_full_suite(
        env_extra={
            "PREPUSH_200_HOSTNAME": _real_short_hostname(),
            "PREPUSH_LOAD_OVERRIDE_LOCAL": "50 24",
        },
    )
    assert result.returncode != 0, result
    assert "probed hosts:" in result.stderr, (
        f"expected the probe trail in the refusal, got {result.stderr!r}"
    )
    # CAPACITY rows only. An `identity` row (h201c, the .201 gate-runner
    # container) has no ssh target by construction -- it confers identity and is
    # never an execution target -- so probing it would be meaningless work, and
    # asserting it appears here would pin a bug rather than a behavior.
    for label in _capacity_row_labels():
        assert f"{label}=" in result.stderr, (
            f"capacity row {label} is in the committed table but absent from "
            f"the refusal's probe trail: {result.stderr!r}"
        )
    assert "STUB-PYTEST-INVOKED" not in result.stdout, (
        "the guard must refuse BEFORE pytest is ever invoked"
    )


def _real_short_hostname() -> str:
    return subprocess.run(
        ["hostname", "-s"], capture_output=True, text=True, check=True
    ).stdout.strip()


# =============================================================================
# Containment: a unit test must never spend a lab host (OMN-16991 / OMN-17435)
# =============================================================================


def test_the_heavy_harness_never_dispatches_a_real_lab_run(tmp_path: Path) -> None:
    """A unit test must not take a lab host's exclusive slot for an hour.

    Before OMN-17435 this repo's hook had no lab-dispatch seam at all, so the
    harnesses above were contained by construction. That containment is now
    gone. The equivalent was observed live in omnibase_infra on 2026-08-30,
    minutes after its host scan was fixed: the next `pytest` shipped a real git
    bundle to `omnibook`, took its LOCK, and started a full suite there -- with
    the remote wrapper's ORIGIN naming the test process. That is the
    OMN-16425/OMN-16489 F-01 recursion in distributed form, reached from a test
    rather than a push.

    Proven by shadowing `ssh`/`scp` on PATH and asserting that nothing in the
    heavy path ever addresses a lab target. `git fetch` may legitimately use
    ssh for `origin`, so the witness records lab targets only.
    """
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    witness = tmp_path / "lab-calls"
    for name in ("ssh", "scp"):
        stub = stub_bin / name
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'case " $* " in *jonah@*) echo "{name} $*" >> "{witness}" ;; esac\n'
            "exit 1\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)

    env = dict(os.environ)
    for leaky in (
        "PREPUSH_FULL_SUITE",
        "PREPUSH_ALLOW_LOCAL_FULL_SUITE",
        "ENABLE_SMART_TESTS",
        "PREPUSH_ADJACENCY",
        "PREPUSH_PYTEST_ARGS",
        "ONEX_PREPUSH_HOOK_ACTIVE",
    ):
        env.pop(leaky, None)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    env["PREPUSH_FULL_SUITE"] = "1"
    _force_undesignated_host(env)

    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode != 0, (
        "the de-designated heavy path must still refuse; got exit "
        f"{result.returncode}. stderr={result.stderr!r}"
    )
    assert not witness.exists(), (
        "the hook addressed a lab host from inside the test harness: "
        f"{witness.read_text()!r}"
    )


def test_both_hook_harnesses_apply_the_lab_isolation() -> None:
    """Static pin so a new harness cannot quietly reintroduce live dispatch.

    The behavioral test above only covers the harness it drives; this covers
    every subprocess call site in the two files that run the real hook. It
    counts rather than spot-checks, because the failure mode is an ADDED call
    site that forgot the isolation, not an edited one.
    """
    for path in (
        Path(__file__),
        REPO_ROOT
        / "tests"
        / "scripts"
        / "test_prepush_hook_recursion_and_env_guard.py",
    ):
        text = path.read_text(encoding="utf-8")
        hook_runs = text.count('["bash", str(HOOK_SCRIPT)]')
        assert hook_runs > 0, f"{path.name}: expected at least one hook subprocess"
        applications = text.count("network_free_lab_env()") + text.count(
            "_force_undesignated_host("
        )
        assert applications >= hook_runs, (
            f"{path.name}: {hook_runs} hook subprocess call site(s) but only "
            f"{applications} application(s) of the lab isolation -- a harness "
            "that omits it dispatches a real remote run"
        )
