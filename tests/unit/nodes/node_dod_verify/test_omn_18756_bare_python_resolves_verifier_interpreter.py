# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18756: a bare ``python3`` in a check is the VERIFIER's interpreter.

**What was measured.** The scheduled evidence-autoclose sweep reported three
failing checks on OMN-18426. One of them,
``ac1-ac2-hook-on-all-fifteen-default-branches``, died in 31 ms with
``ModuleNotFoundError: No module named 'yaml'`` before it read a single
repository (omnibase_infra run ``35379376978``, the OMN-16788 divergence
diagnostic). The contract's check is a heredoc opening ``python3 - <<'PY'``
that imports ``yaml``; nothing about it is wrong.

**Why it failed.** ``_run_command_check`` shells every command check through
``bash -o pipefail -c`` with an inherited ``PATH``, so a bare ``python3``
resolves whatever the host happens to name. The sweep dispatches the verifier
by ABSOLUTE path -- ``Path(sys.executable).parent / "onex"`` -- precisely so
the verifier's environment is a property of how the sweep was composed; but
invoking an entrypoint never activates its venv, so the dispatch venv's ``bin``
was not on ``PATH`` and ``python3`` fell through to the runner's system
interpreter. That interpreter has no PyYAML. The dispatch venv does: the same
run installed ``pyyaml==6.0.3`` into it. The verifier was itself running on an
interpreter that imports ``yaml`` at module scope while handing its checks one
that could not.

**Why the collaborator saw zero failures.** On an operator Mac the ambient
``python3`` happens to carry PyYAML, so the identical check passes locally.
The verdict was a property of the host, not of the work.

**The two halves.** The interpreter a bare-python check resolves is bound to
the verifier's own (AC1/AC2), and a module missing from THAT interpreter is
recorded as a typed environment cause rather than as a defect in the ticket
(AC3), on the OMN-16788 shape its four siblings already use. Nothing is
relaxed: the new cause is strictly MORE blocking than the FAILED it replaces
(AC4), and the identification is first-hand rather than a grep of the check's
own output, which a product under adjudication could forge (AC5).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    EnumEvidenceUnverifiableCause,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    _BARE_PYTHON_INVOCATION_RE,
    _CHECK_TIMEOUT_ENV,
    _VERIFIER_ENV_FAILURE_MARKER,
    EvidenceCollector,
)

pytestmark = pytest.mark.unit


#: A module name no environment carries. Used to drive the AC3 classification
#: without depending on which third-party packages happen to be installed.
_ABSENT_MODULE = "zzz_absent_module_omn18756"

#: A module every CPython carries, used as the AC5 negative control: a check
#: that PRINTS a failure to import it is lying, and must stay FAILED.
_PRESENT_MODULE = "json"


def _item(command: str, *, item_id: str = "ac1-ac2-hook") -> dict[str, object]:
    """A dod_evidence item in its real shape, carrying one command check."""
    return {
        "id": item_id,
        "description": "read AC1 and AC2 live from every default branch",
        "checks": [{"check_type": "command", "check_value": command}],
    }


def _strip_verifier_bin_from_path(monkeypatch: pytest.MonkeyPatch) -> str:
    """Reproduce the sweep runner's PATH: the verifier's own bin is absent.

    This is not a contrivance. The sweep invokes ``<venv>/bin/onex`` by
    absolute path and never activates the venv, so on the runner the dispatch
    venv's ``bin`` is genuinely off ``PATH``. Under ``uv run pytest`` it is ON
    it, which is exactly why this defect is invisible from a local verifier
    run and had to be reproduced rather than observed.

    Returns the stripped PATH so a caller can assert against it.
    """
    verifier_bin = str(Path(sys.executable).parent)
    stripped = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and entry != verifier_bin
    )
    monkeypatch.setenv("PATH", stripped)
    return stripped


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> EvidenceCollector:
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.delenv(_CHECK_TIMEOUT_ENV, raising=False)
    # These items bind to no PR; the OMN-14207 live check would shell out to
    # `gh` for a merge state irrelevant to interpreter resolution.
    monkeypatch.setenv("DOD_VERIFY_LIVE_PR_CHECK", "0")
    return EvidenceCollector()


def _result(
    status: EnumEvidenceCheckStatus,
    *,
    cause: EnumEvidenceUnverifiableCause | None = None,
    proof_class: EnumCheckProofClass = EnumCheckProofClass.BEHAVIOR,
    evidence_id: str = "ac1-ac2-hook",
) -> ModelEvidenceCheckResult:
    return ModelEvidenceCheckResult(
        evidence_id=evidence_id,
        description="read AC1 and AC2 live from every default branch",
        status=status,
        unverifiable_cause=cause,
        proof_class=proof_class,
    )


def _verdict(checks: list[ModelEvidenceCheckResult]) -> ModelDodVerifyState:
    """Drive the handler's typed entry point over caller-supplied results."""
    return HandlerDodVerify()._handle_typed(
        ModelDodVerifyStartCommand(ticket_id="OMN-18426"),
        evidence_results=checks,
    )


# ---------------------------------------------------------------------------
# RED-first premise — the defect's mechanism, stated before anything is claimed
# ---------------------------------------------------------------------------


def test_the_shell_resolves_a_different_interpreter_than_the_verifier_runs_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The premise, asserted against the shell directly and not through the fix.

    With the verifier's own bin off ``PATH`` -- the runner's real state -- a
    bare ``python3`` resolves somewhere else, or nowhere. If this does not
    hold, the rest of the file is testing a supposed defect.
    """
    import shutil

    stripped = _strip_verifier_bin_from_path(monkeypatch)
    resolved = shutil.which("python3", path=stripped)
    assert resolved is None or Path(resolved).parent != Path(sys.executable).parent


# ---------------------------------------------------------------------------
# AC1 — a bare python check runs on the verifier's own interpreter
# ---------------------------------------------------------------------------


def test_a_bare_python_check_resolves_the_verifiers_interpreter(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix, measured where the defect was: the runner's PATH shape."""
    _strip_verifier_bin_from_path(monkeypatch)
    ok, msg = collector._run_command_check(
        {"check_value": 'python3 -c "import sys; print(sys.executable)"'},
        "OMN-18426",
    )
    assert ok, msg
    printed = msg.split(": ", 1)[1].strip()
    assert Path(printed).parent == Path(sys.executable).parent


def test_the_heredoc_form_the_contract_actually_uses_resolves_it_too(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python3 - <<'PY'`` is the shape OMN-18426's own check is written in."""
    _strip_verifier_bin_from_path(monkeypatch)
    ok, msg = collector._run_command_check(
        {"check_value": ("python3 - <<'PY'\nimport sys\nprint(sys.executable)\nPY")},
        "OMN-18426",
    )
    assert ok, msg
    printed = msg.split(": ", 1)[1].strip()
    assert Path(printed).parent == Path(sys.executable).parent


def test_the_original_yaml_import_now_succeeds(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact failing line from run 35379376978, with nothing else changed.

    ``yaml`` is a declared dependency of the verifier's own distribution and
    is imported at module scope by ``evidence_collector`` itself, so an
    interpreter the verifier hands a check must be able to import it.
    """
    _strip_verifier_bin_from_path(monkeypatch)
    ok, msg = collector._run_command_check(
        {"check_value": "python3 - <<'PY'\nimport yaml\nprint(yaml.__name__)\nPY"},
        "OMN-18426",
    )
    assert ok, msg
    assert "No module named" not in msg


def test_a_non_python_check_keeps_the_path_it_was_given(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control for AC1: the routing is not a blanket PATH rewrite.

    A check that resolves no interpreter must see the environment it would
    have seen before this change, or the blast radius is every check rather
    than the ones this ticket is about.
    """
    stripped = _strip_verifier_bin_from_path(monkeypatch)
    ok, msg = collector._run_command_check({"check_value": 'printf "%s" "$PATH"'}, "X")
    assert ok, msg
    assert (
        msg.split(": ", 1)[1].strip() == stripped[: len(msg.split(": ", 1)[1].strip())]
    )
    assert not msg.split(": ", 1)[1].startswith(str(Path(sys.executable).parent))


# ---------------------------------------------------------------------------
# AC2 — the routing reaches exactly the commands that resolve a bare python
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 - <<'PY'\nimport yaml\nPY",
        'python -c "import yaml"',
        "python3 script.py",
        "gh api repos/o/r --jq .x | python3 -c 'import sys; sys.exit(0)'",
        "cd /tmp && python3 -c 'pass'",
        "if true; then python3 -c 'pass'; fi",
    ],
)
def test_a_bare_interpreter_invocation_is_routed(command: str) -> None:
    assert _BARE_PYTHON_INVOCATION_RE.search(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -c 'import yaml'",
        "uv run pytest tests/unit -q",
        "gh api repos/o/r --jq '.name'",
        "./run_python.sh",
        "grep -rn python src/",
        "pythonx --version",
        "/usr/bin/python3 -c 'pass'",
    ],
)
def test_anything_else_is_untouched(command: str) -> None:
    """Negative control for AC2.

    ``uv run python`` resolves the project environment on purpose and must not
    be re-pointed; an absolute interpreter path names what it names; and a
    word merely CONTAINING "python" is not an invocation of one.
    """
    assert _BARE_PYTHON_INVOCATION_RE.search(command) is None


# ---------------------------------------------------------------------------
# AC3 — a module missing from the verifier's own interpreter is an
#       environment finding, named as such
# ---------------------------------------------------------------------------


def test_a_module_absent_from_the_verifiers_interpreter_is_skipped_not_failed(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    _strip_verifier_bin_from_path(monkeypatch)
    result = collector._check_evidence_item(
        _item(f"python3 -c 'import {_ABSENT_MODULE}'"), "OMN-18426"
    )
    assert result.status is EnumEvidenceCheckStatus.SKIPPED
    assert (
        result.unverifiable_cause is EnumEvidenceUnverifiableCause.VERIFIER_ENVIRONMENT
    )
    assert _ABSENT_MODULE in result.message


def test_the_runner_marks_it_itself_rather_than_the_caller_re_deriving_it(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker is emitted where the routing fact is FIRST-HAND.

    ``_run_command_check`` is the only frame that knows it re-pointed the
    interpreter, so it is the only frame entitled to say the failure is about
    the environment -- the ``_HERMETIC_ENV_FAILURE_MARKER`` rule, one axis over.
    """
    _strip_verifier_bin_from_path(monkeypatch)
    ok, msg = collector._run_command_check(
        {"check_value": f"python3 -c 'import {_ABSENT_MODULE}'"}, "OMN-18426"
    )
    assert ok is False
    assert msg.startswith(_VERIFIER_ENV_FAILURE_MARKER)


def test_an_ordinary_assertion_failure_is_still_failed(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control for AC3: routing a check does not soften its verdict.

    A python check that runs and finds the product wanting is a product
    failure, and re-pointing its interpreter must not change that.
    """
    _strip_verifier_bin_from_path(monkeypatch)
    result = collector._check_evidence_item(
        _item("python3 -c 'raise SystemExit(1)'"), "OMN-18426"
    )
    assert result.status is EnumEvidenceCheckStatus.FAILED
    assert result.unverifiable_cause is None


# ---------------------------------------------------------------------------
# AC4 — the new cause opens no flip path
# ---------------------------------------------------------------------------


def test_a_verdict_carrying_only_this_cause_is_never_verified() -> None:
    verdict = _verdict(
        [
            _result(
                EnumEvidenceCheckStatus.SKIPPED,
                cause=EnumEvidenceUnverifiableCause.VERIFIER_ENVIRONMENT,
            )
        ]
    )
    assert verdict.status is not EnumDodVerifyStatus.VERIFIED


def test_the_cause_is_neither_probative_nor_behaviour_proving_nor_removed() -> None:
    """Each conjunct of the OMN-16821 flip predicate, asserted separately.

    ``verified + non_probative == total AND behavior_proving > 0`` must still
    refuse, and the check must stay in the denominator -- a cause that shrank
    the total would be a flip path wearing an honest name.
    """
    verdict = _verdict(
        [
            _result(
                EnumEvidenceCheckStatus.SKIPPED,
                cause=EnumEvidenceUnverifiableCause.VERIFIER_ENVIRONMENT,
            ),
            _result(EnumEvidenceCheckStatus.VERIFIED, evidence_id="dod-other"),
        ]
    )
    assert verdict.total_checks == 2
    assert verdict.verified_count == 1
    assert verdict.status is not EnumDodVerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# AC5 — the identification is first-hand, never the check's own claim
# ---------------------------------------------------------------------------


def test_a_check_that_prints_a_forged_module_error_is_still_failed(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A product under adjudication must not be able to mint its own cause.

    The deciding fact is this process re-resolving the named module in its OWN
    interpreter. A check that prints the words for a module the verifier can
    plainly import has said nothing about the environment, and stays FAILED.
    """
    _strip_verifier_bin_from_path(monkeypatch)
    forged = (
        'python3 -c "import sys; '
        f"sys.stderr.write(\\\"ModuleNotFoundError: No module named '{_PRESENT_MODULE}'\\\"); "
        'sys.exit(1)"'
    )
    result = collector._check_evidence_item(_item(forged), "OMN-18426")
    assert result.status is EnumEvidenceCheckStatus.FAILED
    assert result.unverifiable_cause is None


def test_an_unrouted_check_printing_the_words_is_still_failed(
    collector: EvidenceCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The routing fact is a precondition of the classification, not a hint.

    ``echo`` resolves no interpreter, so this process re-pointed nothing and
    has no first-hand standing to call the failure environmental -- whatever
    the output says.
    """
    _strip_verifier_bin_from_path(monkeypatch)
    result = collector._check_evidence_item(
        _item(
            'echo "ModuleNotFoundError: No module named '
            f"'{_ABSENT_MODULE}'\" >&2; exit 1"
        ),
        "OMN-18426",
    )
    assert result.status is EnumEvidenceCheckStatus.FAILED
    assert result.unverifiable_cause is None
