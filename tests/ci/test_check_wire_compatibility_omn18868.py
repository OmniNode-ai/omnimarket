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
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
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

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The last release cut BEFORE ``published_at`` entered the delegate-skill
#: request model. ``v0.4.137`` is the first release that carries it, so this is
#: the exact boundary the OMN-18852 outage straddled.
PRE_PUBLISHED_AT_RELEASE = "v0.4.133"

WIRE_MODULE = "src/omnimarket/models/delegation/wire/model_delegate_skill_request.py"

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
    tags = subprocess.run(
        ["git", "tag", "--list", PRE_PUBLISHED_AT_RELEASE],
        cwd=str(REPO_ROOT),
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        check=False,
    )
    if PRE_PUBLISHED_AT_RELEASE not in tags.stdout.decode():
        pytest.skip(f"{PRE_PUBLISHED_AT_RELEASE} not fetched in this checkout")

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
    """
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
