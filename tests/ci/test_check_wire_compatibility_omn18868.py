# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Controls for the wire-compatibility gate (OMN-18868).

Every test here is either a FALSIFIER -- a tree the gate must refuse -- or a
POSITIVE CONTROL -- a tree it must pass. A gate with only falsifiers is a
blanket freeze that gets switched off; a gate with only positive controls has
never been proven able to fail.

Two of them run against this repository's REAL history rather than a fixture,
because the defect OMN-18868 exists for is a real one with a real release
boundary: ``published_at`` entered ``ModelDelegateSkillRequest`` after
``v0.4.133`` and the deployed consumer at that release refused it live with
``published_at: Extra inputs are not permitted``. A fixture can show the
mechanism; only the real tags show it catching the actual incident.

WHERE THE TWO REAL-HISTORY CONTROLS ACTUALLY EXECUTE

They need tags and an unshallow history, and the shard job has neither: it
checks out at depth 1 with no tags, so ``git tag --merged`` answers nothing
there. That is a fact about the checkout, not a verdict about the tree, and
the controls must not read as a refusal because of it -- an earlier revision
of this file let one of them do exactly that, and it reddened the pull
request with ``WIRE_COMPAT_RELEASE_UNRESOLVABLE`` while the gate itself was
sound.

So both skip on a checkout with no release history, both are recorded in
``config/skip_count_baseline.yaml`` with that provenance, and both EXECUTE in
the ``Wire Compatibility Gate`` job, which checks out at ``fetch-depth: 0``
and is a strict gate job. That job asserts they ran rather than trusting the
exit status, because two skipped tests also exit zero.
:func:`test_the_gate_job_runs_the_real_history_controls` is what keeps the
arrangement from being quietly undone, and unlike the two controls it runs in
every checkout.
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "ci"))

from check_wire_compatibility import (
    OUTCOME_NO_WIRE_CHANGE,
    OUTCOME_PASS,
    OUTCOME_PROBE_FAILED,
    OUTCOME_REFUSED,
    OUTCOME_RELEASE_UNREADABLE,
    OUTCOME_RELEASE_UNRESOLVABLE,
    WIRE_MODEL_ROOTS,
    main,
)
from ci_summary_gate import STRICT_GATE_JOBS

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The last release cut BEFORE ``published_at`` entered the delegate-skill
#: request model. ``v0.4.137`` is the first release that carries it, so this is
#: the exact boundary the OMN-18852 outage straddled.
PRE_PUBLISHED_AT_RELEASE = "v0.4.133"

WIRE_MODULE = "src/omnimarket/models/delegation/wire/model_delegate_skill_request.py"

#: Where a reader of a skipped real-history control is sent. A skip whose
#: reason does not say where the assertion DOES run is indistinguishable from
#: an assertion nobody makes.
GATE_JOB_NAME = "Wire Compatibility Gate"

_SKIP_REASON = (
    "this checkout carries no release history ({detail}); the two real-history "
    "controls execute in the " + repr(GATE_JOB_NAME) + " job, which checks out "
    "at fetch-depth 0 and asserts they were not skipped"
)


def _tags_merged_into_head() -> list[str]:
    """Release tags reachable from HEAD, or an empty list in a thin checkout."""
    result = subprocess.run(
        ["git", "tag", "--merged", "HEAD"],
        cwd=str(REPO_ROOT),
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [
        line.strip() for line in result.stdout.decode().splitlines() if line.strip()
    ]


def _require_tag(tag: str) -> None:
    """Skip unless *tag* itself is present in this checkout.

    The anchored replay passes ``--tag-anchor`` explicitly, so it needs that
    one tag's objects and not the ancestry.
    """
    listed = subprocess.run(
        ["git", "tag", "--list", tag],
        cwd=str(REPO_ROOT),
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        check=False,
    )
    if tag not in listed.stdout.decode():
        pytest.skip(_SKIP_REASON.format(detail=tag + " is not fetched"))


def _require_reachable_release() -> None:
    """Skip unless a release tag is REACHABLE from HEAD.

    ``git tag --merged`` is what the gate resolves its consumer with, so this
    is the gate's own precondition rather than a proxy for it. A shallow clone
    fails it even with every tag fetched, because the ancestry is truncated.
    """
    if not _tags_merged_into_head():
        pytest.skip(_SKIP_REASON.format(detail="no release tag is merged into HEAD"))


FIXTURE_PACKAGE = "wirefixture"
FIXTURE_MODULE_PATH = f"src/{FIXTURE_PACKAGE}/wire/model_thing.py"
FIXTURE_WIRE_ROOT = f"src/{FIXTURE_PACKAGE}/wire"


def _model_source(*, fields: str, extra: str) -> str:
    return (
        "from __future__ import annotations\n"
        "\n"
        "from pydantic import BaseModel, ConfigDict\n"
        "\n"
        "\n"
        "class ModelThing(BaseModel):\n"
        f'    model_config = ConfigDict(extra="{extra}")\n'
        "\n"
        f"{fields}"
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.email=gate@omninode.ai",
            "-c",
            "user.name=wire compatibility gate test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        # Git exports GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE into every hook
        # environment and they OVERRIDE both ``cwd=`` and ``git -C``. Without
        # this scrub a fixture run under a pre-push hook would build its
        # "release" commits in the REAL invoking worktree rather than in
        # tmp_path (OMN-14891 / OMN-18434).
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


def _write(repo: Path, relative: str, body: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


@pytest.fixture
def wire_repo(tmp_path: Path) -> Path:
    """A git repository with one importable wire package and no release yet."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    _write(repo, f"src/{FIXTURE_PACKAGE}/__init__.py", "")
    _write(repo, f"src/{FIXTURE_PACKAGE}/wire/__init__.py", "")
    return repo


def _release(repo: Path, *, tag: str, fields: str, extra: str) -> None:
    """Commit a wire model and tag it, making it 'the released consumer'."""
    _write(repo, FIXTURE_MODULE_PATH, _model_source(fields=fields, extra=extra))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"release {tag}")
    _git(repo, "tag", tag)


def _producer(repo: Path, *, fields: str, extra: str) -> None:
    """Rewrite the WORKING TREE's wire model -- the producer under review."""
    _write(repo, FIXTURE_MODULE_PATH, _model_source(fields=fields, extra=extra))


def _run(repo: Path, *, changed: str = FIXTURE_MODULE_PATH) -> int:
    return main(
        [
            "--repo-root",
            str(repo),
            "--changed-file",
            changed,
            "--wire-root",
            FIXTURE_WIRE_ROOT,
        ]
    )


# --------------------------------------------------------------------------
# Falsifiers -- trees the gate MUST refuse
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_new_field_absent_from_the_released_consumer_is_refused(
    wire_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The OMN-18852 shape: a field the released extra-forbid model refuses.

    Falsifier: the gate passes, which is the defect it exists for.
    """
    _release(wire_repo, tag="v1.0.0", fields="    prompt: str\n", extra="forbid")
    _producer(
        wire_repo,
        fields="    prompt: str\n    published_at: str | None = None\n",
        extra="forbid",
    )

    assert _run(wire_repo) == 1

    captured = capsys.readouterr()
    assert OUTCOME_REFUSED in captured.err
    assert "published_at" in captured.err
    assert "v1.0.0" in captured.err


@pytest.mark.unit
def test_dropping_a_field_the_release_still_requires_is_refused(
    wire_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The mirror-image break: the producer stops emitting a required key.

    A deployed consumer refuses that payload at the same decode boundary, for
    the same reason, so a gate that graded only added fields would be half a
    gate. Falsifier: a removal passes.
    """
    _release(
        wire_repo,
        tag="v1.0.0",
        fields="    prompt: str\n    correlation_id: str\n",
        extra="forbid",
    )
    _producer(wire_repo, fields="    prompt: str\n", extra="forbid")

    assert _run(wire_repo) == 1

    captured = capsys.readouterr()
    assert OUTCOME_REFUSED in captured.err
    assert "correlation_id" in captured.err


@pytest.mark.unit
def test_release_is_resolved_from_the_release_never_from_the_tree(
    wire_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC3: a tree that accepts its own payload does not make the gate pass.

    This is THE test that separates this gate from the 771 golden chains that
    could not see OMN-18852. The working tree's own model tolerates extras and
    already declares the new field, so every same-commit check -- unit,
    integration, golden chain -- is green. The RELEASED model forbids extras
    and has never heard of the field. The gate must still refuse.

    The first assertion proves the tree really is blind, so that a later
    reading of this test cannot mistake the second assertion for a restatement
    of something the tree already caught.
    """
    _release(wire_repo, tag="v2.0.0", fields="    prompt: str\n", extra="forbid")
    _producer(
        wire_repo,
        fields="    prompt: str\n    published_at: str | None = None\n",
        extra="ignore",
    )

    # The tree, checked against itself, is perfectly happy -- the same-commit
    # blindness, demonstrated rather than asserted.
    tree_check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]);"
            f"from {FIXTURE_PACKAGE}.wire.model_thing import ModelThing;"
            "ModelThing.model_validate("
            "{'prompt': 'x', 'published_at': 'x'})",
            str(wire_repo / "src"),
        ],
        capture_output=True,
        check=False,
    )
    assert tree_check.returncode == 0, tree_check.stderr.decode()

    assert _run(wire_repo) == 1

    captured = capsys.readouterr()
    assert OUTCOME_REFUSED in captured.err
    assert "published_at" in captured.err
    assert "v2.0.0" in captured.err


@pytest.mark.integration
def test_real_published_at_replay_against_the_release_that_predates_it() -> None:
    """AC1/AC2: the actual incident, replayed against the actual release.

    ``omnimarket#2692`` put ``published_at`` on the delegate-skill request. The
    consumer deployed at ``v0.4.133`` refused it live at
    2026-09-20T01:43:38Z with ``published_at: Extra inputs are not permitted``.
    The gate must reproduce that refusal from this repository's own history.

    Falsifier: the gate passes on a payload shape a deployed consumer was
    measured refusing.
    """
    _require_tag(PRE_PUBLISHED_AT_RELEASE)

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "ci" / "check_wire_compatibility.py"),
            "--repo-root",
            str(REPO_ROOT),
            "--tag-anchor",
            PRE_PUBLISHED_AT_RELEASE,
            "--changed-file",
            WIRE_MODULE,
        ],
        capture_output=True,
        check=False,
    )
    stderr = completed.stderr.decode()
    assert completed.returncode == 1, completed.stdout.decode() + stderr
    assert OUTCOME_REFUSED in stderr
    assert "published_at" in stderr
    assert PRE_PUBLISHED_AT_RELEASE in stderr
    # The live terminal's own words, so a reader can match the gate's output
    # against the incident record without translating between them.
    assert "Extra inputs are not permitted" in stderr


# --------------------------------------------------------------------------
# Positive controls -- trees the gate MUST pass
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_field_the_released_consumer_already_declares_passes(
    wire_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC4: consumer-first done correctly is not refused.

    Falsifier: every wire change is refused, which makes the gate a blanket
    freeze and gets it disabled.
    """
    _release(
        wire_repo,
        tag="v1.0.0",
        fields="    prompt: str\n    published_at: str | None = None\n",
        extra="forbid",
    )
    _producer(
        wire_repo,
        fields="    prompt: str\n    published_at: str | None = None\n"
        "    note: str = ''\n",
        extra="forbid",
    )

    # ``note`` is new in the tree AND absent from the release, so this control
    # would pass vacuously if the gate ignored added fields. Prove it does not:
    # the same tree against a release WITHOUT ``note`` is refused.
    assert _run(wire_repo) == 1
    capsys.readouterr()

    _producer(
        wire_repo,
        fields="    prompt: str\n    published_at: str | None = None\n",
        extra="forbid",
    )
    assert _run(wire_repo) == 0
    assert OUTCOME_PASS in capsys.readouterr().out


@pytest.mark.unit
def test_released_consumer_that_tolerates_extras_passes(
    wire_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC4: a release whose model ignores extras absorbs the new field."""
    _release(wire_repo, tag="v1.0.0", fields="    prompt: str\n", extra="ignore")
    _producer(
        wire_repo,
        fields="    prompt: str\n    published_at: str | None = None\n",
        extra="ignore",
    )

    assert _run(wire_repo) == 0
    assert OUTCOME_PASS in capsys.readouterr().out


@pytest.mark.unit
def test_a_change_outside_the_wire_package_is_not_graded(
    wire_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gate that graded every change would be refused adoption on cost."""
    _release(wire_repo, tag="v1.0.0", fields="    prompt: str\n", extra="forbid")
    _write(wire_repo, "src/wirefixture/unrelated.py", "VALUE = 1\n")

    assert _run(wire_repo, changed="src/wirefixture/unrelated.py") == 0
    assert OUTCOME_NO_WIRE_CHANGE in capsys.readouterr().out


@pytest.mark.unit
def test_a_module_the_release_does_not_contain_is_noted_not_refused(
    wire_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A brand-new wire module has no deployed consumer to skew against.

    Refusing it would make the first pull request of any new wire contract
    impossible to land, which is the blanket-freeze failure in another shape.
    """
    _write(wire_repo, "README.md", "fixture\n")
    _git(wire_repo, "add", "-A")
    _git(wire_repo, "commit", "-q", "-m", "release v1.0.0")
    _git(wire_repo, "tag", "v1.0.0")
    _producer(wire_repo, fields="    prompt: str\n", extra="forbid")

    assert _run(wire_repo) == 0
    captured = capsys.readouterr()
    assert OUTCOME_PASS in captured.out
    assert "no module" in captured.out


@pytest.mark.integration
def test_the_current_release_decodes_todays_wire_models() -> None:
    """AC4 against reality: dev is compatible with the live released consumer.

    If this fails, the gate is not wrong -- the fleet has a live consumer-first
    violation on dev, which is exactly what the gate is for. It is asserted
    here so that a red reading is unambiguous rather than dismissed as fixture
    noise.

    UNRESOLVABLE IS NOT THE SAME READING. A checkout with no reachable release
    makes the gate answer ``WIRE_COMPAT_RELEASE_UNRESOLVABLE``, which is the
    correct fail-closed answer for the GATE and the wrong answer for THIS
    control: it says nothing about dev's compatibility. Distinguishing the two
    is the whole point of the precondition.
    """
    _require_reachable_release()

    changed: list[str] = []
    for wire_root in WIRE_MODEL_ROOTS:
        for path in sorted((REPO_ROOT / wire_root).glob("model_*.py")):
            changed += ["--changed-file", f"{wire_root}/{path.name}"]

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "ci" / "check_wire_compatibility.py"),
            "--repo-root",
            str(REPO_ROOT),
            *changed,
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, (
        completed.stdout.decode() + completed.stderr.decode()
    )


# --------------------------------------------------------------------------
# Fail-closed arms (AC5) -- the gate must never read as green when it did not run
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_no_release_tag_fails_closed(
    wire_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC5: an unresolvable release is UNPROVEN, never compatible."""
    _write(
        wire_repo,
        FIXTURE_MODULE_PATH,
        _model_source(fields="    prompt: str\n", extra="forbid"),
    )
    _git(wire_repo, "add", "-A")
    _git(wire_repo, "commit", "-q", "-m", "no tags here")

    assert _run(wire_repo) == 1
    assert OUTCOME_RELEASE_UNRESOLVABLE in capsys.readouterr().err


@pytest.mark.unit
def test_a_release_carrying_no_source_tree_fails_closed(
    wire_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC5: an unreadable release must not read as 'every model is new'.

    The release is tagged on a commit that carries no ``src/`` at all, so the
    extraction yields nothing. The permissive reading of nothing -- "no
    released consumer defines this model, so none can refuse it" -- is exactly
    the quiet green this epic exists to end, and it is only one ``git archive``
    failure away from being the gate's answer for EVERY model at once.

    Note the ordering: ``git add README.md`` and not ``git add -A``, so the
    fixture's own package stubs stay out of the tagged tree. An earlier draft
    of this test used ``-A`` and tagged a commit that DID carry ``src/``; it
    then measured the ordinary absent-module path and passed for the wrong
    reason.
    """
    _write(wire_repo, "README.md", "fixture\n")
    _git(wire_repo, "add", "README.md")
    _git(wire_repo, "commit", "-q", "-m", "docs only, no source tree")
    _git(wire_repo, "tag", "v1.0.0")
    _producer(wire_repo, fields="    prompt: str\n", extra="forbid")

    assert _run(wire_repo) == 1
    assert OUTCOME_RELEASE_UNREADABLE in capsys.readouterr().err


@pytest.mark.unit
def test_a_released_module_that_will_not_import_fails_closed(
    wire_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC5: an unloadable released model is UNPROVEN, never compatible.

    Falsifier: the gate treats an import failure as 'nothing refused it'.
    """
    _write(
        wire_repo,
        FIXTURE_MODULE_PATH,
        "raise RuntimeError('this release cannot be imported')\n",
    )
    _git(wire_repo, "add", "-A")
    _git(wire_repo, "commit", "-q", "-m", "release v1.0.0")
    _git(wire_repo, "tag", "v1.0.0")
    _producer(wire_repo, fields="    prompt: str\n", extra="forbid")

    assert _run(wire_repo) == 1
    assert OUTCOME_PROBE_FAILED in capsys.readouterr().err


@pytest.mark.unit
def test_the_probe_refuses_a_module_served_from_outside_its_src_root(
    tmp_path: Path,
) -> None:
    """The anti-self-comparison guard, exercised rather than assumed.

    If an editable install or a stale ``sys.modules`` entry ever served the
    working tree to the RELEASE probe, the gate would compare the tree against
    itself and pass everything. The probe refuses that outcome; this proves the
    refusal is live, because a guard no test has ever tripped is a guard nobody
    knows still works.
    """
    empty_root = tmp_path / "empty-src"
    empty_root.mkdir()
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "ci" / "_wire_compat_probe.py"),
            "--src-root",
            str(empty_root),
            "--module",
            "omnimarket.models.delegation.wire.model_delegate_skill_request",
            "--mode",
            "describe",
            "--out",
            str(tmp_path / "out.json"),
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "not under" in completed.stderr.decode()
    assert not (tmp_path / "out.json").exists()


# --------------------------------------------------------------------------
# Drift guard on the declared set
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_every_wire_package_in_the_tree_is_declared() -> None:
    """A new wire package must be enrolled deliberately, not by accident.

    The repository has no marker base class and no wire registry, so the
    graded set is a written declaration. This test is what keeps the
    declaration honest: adding a ``wire/`` package with pydantic payload
    modules turns it RED until someone decides, in writing, whether the new
    package is graded.
    """
    discovered = {
        str(path.parent.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "src").glob("**/wire/model_*.py")
    }
    assert discovered == set(WIRE_MODEL_ROOTS)


# --------------------------------------------------------------------------
# The wiring control -- the one test here that runs in EVERY checkout
# --------------------------------------------------------------------------

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GATE_JOB_ID = "wire-compatibility-gate"
GATE_SCRIPT = "scripts/ci/check_wire_compatibility.py"
THIS_TEST_FILE = "tests/ci/test_check_wire_compatibility_omn18868.py"


def _run_steps(job: dict[str, Any]) -> list[str]:
    return [str(step.get("run", "")) for step in job.get("steps", [])]


def assert_gate_job_wired(
    workflow: dict[str, Any], strict_jobs: tuple[str, ...]
) -> None:
    """Raise unless the gate job can actually refuse a pull request.

    Five separate ways this gate could exist and enforce nothing, each with
    its own assertion: the job absent, its name out of the strict set (a
    failure the CI Summary sweep then never reads), the job made conditional
    so a skip reads as success, its checkout thinned so the released consumer
    cannot be resolved, and the real-history controls silently skipped inside
    a job that still exits zero.
    """
    jobs = workflow.get("jobs", {})
    job = jobs.get(GATE_JOB_ID)
    assert job is not None, f"{GATE_JOB_ID} is gone from {WORKFLOW.name}"

    name = job.get("name")
    assert name in strict_jobs, (
        f"{name!r} is not in STRICT_GATE_JOBS, so CI Summary reads a skipped "
        "or absent wire check as success"
    )

    conditional = "the gate job became conditional; a skipped conclusion is then "
    conditional += "indistinguishable from a pass"
    assert "needs" not in job, conditional
    assert "if" not in job, conditional

    checkouts = [
        step.get("with") or {}
        for step in job.get("steps", [])
        if "actions/checkout" in str(step.get("uses", ""))
    ]
    assert checkouts, "the gate job no longer checks the repository out"
    assert checkouts[0].get("fetch-depth") == 0, (
        "the gate job must check out at fetch-depth 0; without the tags the "
        "released consumer is unresolvable and every pull request reddens for "
        "the fetch depth rather than for a wire break"
    )

    runs = _run_steps(job)
    assert any(GATE_SCRIPT in run for run in runs), (
        f"no step in the gate job runs {GATE_SCRIPT}"
    )

    control_runs = [
        run for run in runs if THIS_TEST_FILE in run and "-m integration" in run
    ]
    assert control_runs, (
        "the gate job no longer executes the two real-history controls, and "
        "the shard job skips them, so nothing would run them at all"
    )
    assert any("junit-xml" in run for run in control_runs), (
        "the gate job runs the controls but never reads the report, so two "
        "SKIPPED controls would exit zero and the job would print and pass"
    )


@pytest.mark.unit
def test_the_gate_job_runs_the_real_history_controls() -> None:
    """The gate is wired in as blocking, asserted against the workflow tree.

    Falsifier: the gate exists and enforces nothing -- unregistered,
    conditional, shallow, or running controls that skip.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert_gate_job_wired(workflow, STRICT_GATE_JOBS)


@pytest.mark.unit
@pytest.mark.parametrize(
    "unwire",
    [
        pytest.param(lambda job: job.pop("name"), id="name-dropped"),
        pytest.param(
            lambda job: job.__setitem__("if", "github.event_name == 'push'"),
            id="made-conditional",
        ),
        pytest.param(
            lambda job: job["steps"][0]["with"].__setitem__("fetch-depth", 1),
            id="checkout-thinned",
        ),
        pytest.param(
            lambda job: job["steps"].pop(),
            id="controls-step-removed",
        ),
        pytest.param(
            lambda job: job["steps"][-1].__setitem__(
                "run", "uv run pytest " + THIS_TEST_FILE + " -m integration -q"
            ),
            id="report-not-read",
        ),
    ],
)
def test_an_unwired_gate_job_is_caught(unwire: Any) -> None:
    """The negative control for the test above, on the live workflow.

    Without these, a wiring assertion that had quietly stopped checking
    anything would look exactly like a wired gate.
    """
    workflow = copy.deepcopy(yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))
    unwire(workflow["jobs"][GATE_JOB_ID])
    with pytest.raises(AssertionError):
        assert_gate_job_wired(workflow, STRICT_GATE_JOBS)


# --------------------------------------------------------------------------
# AC6 -- the gate is a REQUIRED context on dev, declared where the fleet reads it
# --------------------------------------------------------------------------

REQUIRED_CHECKS_MANIFEST = REPO_ROOT / ".github" / "required-checks.yaml"


def assert_gate_declared_required_on_dev(manifest: dict[str, Any]) -> None:
    """Raise unless the manifest declares the gate REQUIRED on ``dev``.

    ``.github/required-checks.yaml`` is the declaration the required-check
    skip-vector guard reads and the one ``reconcile_manifest_vs_live.py``
    diffs against live branch protection. A context that is live-required but
    undeclared is drift the reconcile reports; a context declared anything but
    REQUIRED, or declared on ``main`` only, is the advisory shape AC6's
    falsifier names. ``skip_semantics: never`` is what makes the skip guard
    refuse a later edit that gives the job a ``needs``/``if`` skip vector.
    """
    rows = [
        row for row in manifest.get("gates", []) if row.get("name") == GATE_JOB_NAME
    ]
    assert len(rows) == 1, (
        f"{GATE_JOB_NAME!r} must be declared exactly once in "
        f"{REQUIRED_CHECKS_MANIFEST.name}, found {len(rows)}"
    )
    row = rows[0]
    assert row.get("mode") == "REQUIRED", (
        f"{GATE_JOB_NAME!r} is declared {row.get('mode')!r}; anything but "
        "REQUIRED is the advisory shape AC6 refuses"
    )
    assert row.get("branch", "dev") == "dev", (
        f"{GATE_JOB_NAME!r} is declared on {row.get('branch')!r} only; pull "
        "requests land on dev, so that is where it must be required"
    )
    assert row.get("workflow") == WORKFLOW.name
    assert row.get("job_path") == [GATE_JOB_ID]
    assert row.get("skip_semantics") == "never"


@pytest.mark.unit
def test_the_gate_is_declared_required_on_dev() -> None:
    """AC6: the gate is a declared REQUIRED context on dev.

    Falsifier: the check exists and is advisory -- present in the workflow,
    absent from the required set -- which Rule 5 says will be ignored.
    """
    manifest = yaml.safe_load(REQUIRED_CHECKS_MANIFEST.read_text(encoding="utf-8"))
    assert_gate_declared_required_on_dev(manifest)


@pytest.mark.unit
@pytest.mark.parametrize(
    "undeclare",
    [
        pytest.param(
            lambda rows: rows.__setitem__(
                slice(None),
                [r for r in rows if r.get("name") != GATE_JOB_NAME],
            ),
            id="row-removed",
        ),
        pytest.param(
            lambda rows: _gate_row(rows).__setitem__("mode", "ADVISORY"),
            id="made-advisory",
        ),
        pytest.param(
            lambda rows: _gate_row(rows).__setitem__("branch", "main"),
            id="main-only",
        ),
        pytest.param(
            lambda rows: _gate_row(rows).__setitem__("skip_semantics", "neutral_ok"),
            id="skip-tolerated",
        ),
        pytest.param(
            lambda rows: rows.append(dict(_gate_row(rows))),
            id="declared-twice",
        ),
    ],
)
def test_an_undeclared_gate_is_caught(undeclare: Any) -> None:
    """The negative control for the AC6 declaration, on the live manifest."""
    manifest = copy.deepcopy(
        yaml.safe_load(REQUIRED_CHECKS_MANIFEST.read_text(encoding="utf-8"))
    )
    undeclare(manifest["gates"])
    with pytest.raises(AssertionError):
        assert_gate_declared_required_on_dev(manifest)


def _gate_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return next(row for row in rows if row.get("name") == GATE_JOB_NAME)
