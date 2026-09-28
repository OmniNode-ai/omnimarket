# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19527 — the code gate digest compute.

Two kinds of test. The pure ones feed recorded tool output to the compute.
The replay ones run the real tools, with the exact argument lists the lab
container runs (``CODE_GATE_COMMANDS``), over a fixture file placed in a
throwaway repository checkout, and digest what they print:

* AC1: a file carrying an unused import and a ``typing.Any`` yields both.
* AC3: the answers of delegation runs ea0b2d12 and f847f2cc (the delegation
  capability matrix of 2026-09-25, rows in dm-nodes.jsonl) yield the findings
  the lane recorded by hand: ``ruff format`` refusing
  ``model_unresolved_field.py`` for two lines over 88, and ruff F401 for the
  unused ``get_args`` and ``get_origin``. The answers are committed verbatim as
  ``.py.txt`` so this repository's own gates never lint them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_code_gate_digest_compute.handlers.handler_code_gate_digest import (
    HandlerCodeGateDigest,
    digest_code_gates,
    find_any_usages,
    parse_mypy,
    parse_ruff_concise,
)
from omnimarket.nodes.node_code_gate_digest_compute.models.model_code_gate_digest import (
    MAX_DIGEST_CHARS,
    MAX_FINDINGS,
    EnumCodeGate,
    ModelCodeGateDigest,
    ModelCodeGateDigestRequest,
    ModelGateToolOutput,
)
from omnimarket.nodes.node_push_validation_effect.protocols.dtl_container_invocation import (
    CODE_GATE_COMMANDS,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
PATH = "src/pkg/answer.py"


def _out(
    gate: EnumCodeGate, exit_code: int | None, output: str = ""
) -> ModelGateToolOutput:
    return ModelGateToolOutput(gate=gate, exit_code=exit_code, output=output)


def _clean_outputs() -> tuple[ModelGateToolOutput, ...]:
    return (
        _out(EnumCodeGate.RUFF_CHECK, 0, "All checks passed!\n"),
        _out(EnumCodeGate.RUFF_FORMAT, 0, "1 file already formatted\n"),
        _out(EnumCodeGate.MYPY, 0, "Success: no issues found in 1 source file\n"),
    )


def _digest(
    outputs: tuple[ModelGateToolOutput, ...], source: str = "x = 1\n", path: str = PATH
) -> ModelCodeGateDigest:
    return digest_code_gates(
        ModelCodeGateDigestRequest(path=path, source=source, outputs=outputs)
    )


# -- parsers ------------------------------------------------------------------


def test_ruff_concise_lines_are_parsed_with_multi_letter_codes() -> None:
    output = (
        "src/pkg/answer.py:5:8: F401 [*] `os` imported but unused\n"
        "src/pkg/answer.py:12:1: RUF100 [*] Unused `noqa` directive\n"
        "src/pkg/answer.py:14:5: SIM105 Use `contextlib.suppress(ValueError)`\n"
        "Found 3 errors.\n"
        "[*] 2 fixable with the `--fix` option.\n"
    )
    assert parse_ruff_concise(output) == [
        ("src/pkg/answer.py", 5, "F401", "`os` imported but unused"),
        ("src/pkg/answer.py", 12, "RUF100", "Unused `noqa` directive"),
        ("src/pkg/answer.py", 14, "SIM105", "Use `contextlib.suppress(ValueError)`"),
    ]


def test_mypy_errors_are_parsed_and_notes_and_summary_ignored() -> None:
    output = (
        "src/pkg/answer.py:7: error: Function is missing a return type annotation  [no-untyped-def]\n"
        'src/pkg/answer.py:7: note: Use "-> None" if function does not return a value\n'
        "src/pkg/answer.py:9: error: Returning Any from function declared to return int\n"
        "Found 2 errors in 1 file (checked 1 source file)\n"
    )
    assert parse_mypy(output) == [
        (
            "src/pkg/answer.py",
            7,
            "no-untyped-def",
            "Function is missing a return type annotation",
        ),
        (
            "src/pkg/answer.py",
            9,
            "mypy",
            "Returning Any from function declared to return int",
        ),
    ]


def test_any_usages_are_found_for_every_spelling_of_typing_any() -> None:
    source = (
        "import typing\n"  # 1
        "import typing as t\n"  # 2
        "from typing import Any\n"  # 3
        "from typing import Any as Anything\n"  # 4
        "def a(x: Any) -> int: ...\n"  # 5
        "def b(x: typing.Any) -> int: ...\n"  # 6
        "def c(x: t.Any) -> int: ...\n"  # 7
        "def d(x: Anything) -> int: ...\n"  # 8
        "def e(x: int) -> int: ...\n"  # 9
    )
    assert find_any_usages(source) == [5, 6, 7, 8]


def test_any_used_before_its_import_line_is_still_found() -> None:
    source = "def f() -> None:\n    y: Any = 1\n\nfrom typing import Any\n"
    assert find_any_usages(source) == [2]


def test_source_that_does_not_parse_has_no_any_findings() -> None:
    assert find_any_usages("def broken(:\n") == []


# -- the digest -----------------------------------------------------------------


def test_every_gate_clean_is_clean_with_no_fingerprint() -> None:
    digest = _digest(_clean_outputs())
    assert digest.clean is True
    assert digest.infra_error is False
    assert digest.findings == ()
    assert digest.fingerprint == ""
    assert set(digest.gates_run) == set(EnumCodeGate)


def test_ruff_format_refusal_is_one_whole_file_finding() -> None:
    outputs = (
        _clean_outputs()[0],
        _out(
            EnumCodeGate.RUFF_FORMAT,
            1,
            f"Would reformat: {PATH}\n1 file would be reformatted\n",
        ),
        _clean_outputs()[2],
    )
    digest = _digest(outputs)
    assert digest.clean is False
    assert [(f.gate, f.line, f.code) for f in digest.findings] == [
        (EnumCodeGate.RUFF_FORMAT, 0, "format")
    ]


def test_findings_in_other_files_are_not_the_delegated_files_findings() -> None:
    outputs = (
        _clean_outputs()[0],
        _clean_outputs()[1],
        _out(
            EnumCodeGate.MYPY,
            1,
            "src/pkg/other.py:3: error: Name 'z' is not defined  [name-defined]\n"
            f"{PATH}:2: error: Incompatible types in assignment  [assignment]\n",
        ),
    )
    digest = _digest(outputs)
    assert [(f.line, f.code) for f in digest.findings] == [(2, "assignment")]


def test_exit_one_with_nothing_parsed_is_a_finding_never_clean() -> None:
    outputs = (
        _out(EnumCodeGate.RUFF_CHECK, 1, "something ruff said in a new format\n"),
        _clean_outputs()[1],
        _clean_outputs()[2],
    )
    digest = _digest(outputs)
    assert digest.clean is False
    assert digest.findings[0].code == "unparsed"
    assert "something ruff said" in digest.findings[0].message


@pytest.mark.parametrize(
    ("gate", "exit_code"),
    [
        (EnumCodeGate.RUFF_CHECK, None),
        (EnumCodeGate.RUFF_CHECK, 2),
        (EnumCodeGate.RUFF_FORMAT, 2),
        (EnumCodeGate.MYPY, 2),
        (EnumCodeGate.MYPY, 127),
    ],
)
def test_a_tool_that_did_not_run_or_failed_as_a_tool_is_infra_error(
    gate: EnumCodeGate, exit_code: int | None
) -> None:
    outputs = tuple(
        _out(gate, exit_code, "No module named ruff\n") if o.gate is gate else o
        for o in _clean_outputs()
    )
    digest = _digest(outputs)
    assert digest.infra_error is True
    assert digest.clean is False
    assert gate.value in digest.digest_text


def test_a_missing_tool_gate_is_infra_error() -> None:
    digest = _digest(_clean_outputs()[:2])
    assert digest.infra_error is True
    assert digest.clean is False
    assert "mypy" in digest.digest_text


def test_findings_are_capped_the_count_kept_and_the_text_bounded() -> None:
    many = "".join(
        f"{PATH}:{n}:1: F821 Undefined name `name_{n}_{'x' * 150}`\n"
        for n in range(1, 80)
    )
    outputs = (_out(EnumCodeGate.RUFF_CHECK, 1, many), *_clean_outputs()[1:])
    digest = _digest(outputs)
    assert digest.finding_count == 79
    assert len(digest.findings) == MAX_FINDINGS
    assert digest.truncated is True
    assert len(digest.digest_text) <= MAX_DIGEST_CHARS
    assert "more finding" in digest.digest_text


def test_fingerprint_ignores_line_numbers_and_output_order() -> None:
    a = _digest(
        (
            _out(
                EnumCodeGate.RUFF_CHECK,
                1,
                f"{PATH}:5:8: F401 [*] `os` imported but unused\n"
                f"{PATH}:6:8: F401 [*] `re` imported but unused\n",
            ),
            *_clean_outputs()[1:],
        )
    )
    b = _digest(
        (
            _out(
                EnumCodeGate.RUFF_CHECK,
                1,
                f"{PATH}:9:8: F401 [*] `re` imported but unused\n"
                f"{PATH}:7:8: F401 [*] `os` imported but unused\n",
            ),
            *_clean_outputs()[1:],
        )
    )
    c = _digest(
        (
            _out(
                EnumCodeGate.RUFF_CHECK,
                1,
                f"{PATH}:5:8: F401 [*] `os` imported but unused\n",
            ),
            *_clean_outputs()[1:],
        )
    )
    assert a.fingerprint == b.fingerprint != ""
    assert a.fingerprint != c.fingerprint


def test_the_digest_text_names_each_finding_by_line_gate_and_code() -> None:
    source = "from typing import Any\n\nx: Any = 1\n"
    outputs = (
        _out(
            EnumCodeGate.RUFF_CHECK,
            1,
            f"{PATH}:1:20: F401 [*] `typing.Any` imported but unused\n",
        ),
        *_clean_outputs()[1:],
    )
    text = _digest(outputs, source=source).digest_text
    assert f"{PATH}:1: [ruff_check] F401 `typing.Any` imported but unused" in text
    assert f"{PATH}:3: [any_types] ONEX-ANY" in text


def test_the_handler_is_the_pure_function() -> None:
    request = ModelCodeGateDigestRequest(
        path=PATH, source="x = 1\n", outputs=_clean_outputs()
    )
    assert HandlerCodeGateDigest().handle(request) == digest_code_gates(request)


# -- replays over the real tools (AC1, AC3) -------------------------------------

_REPO_PYPROJECT = """\
[project]
name = "gate-fixture"
version = "0"

[tool.ruff]
target-version = "py312"
line-length = 88

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B"]

[tool.mypy]
strict = true
python_version = "3.12"
"""


def _run_gates_in_checkout(
    checkout: Path, rel_path: str
) -> tuple[ModelGateToolOutput, ...]:
    """What the lab container does, run here: each gate over one file, in the checkout."""
    outputs = []
    for gate, argv in CODE_GATE_COMMANDS:
        done = subprocess.run(
            [sys.executable, *argv, rel_path],
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        outputs.append(
            ModelGateToolOutput(
                gate=EnumCodeGate(gate),
                exit_code=done.returncode,
                output=(done.stdout + done.stderr)[-60_000:],
            )
        )
    return tuple(outputs)


def _replay(tmp_path: Path, fixture: str, rel_path: str) -> ModelCodeGateDigest:
    checkout = tmp_path / "checkout"
    (checkout / rel_path).parent.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text(_REPO_PYPROJECT)
    source = (FIXTURES / fixture).read_text()
    shutil.copyfile(FIXTURES / fixture, checkout / rel_path)
    return digest_code_gates(
        ModelCodeGateDigestRequest(
            path=rel_path,
            source=source,
            outputs=_run_gates_in_checkout(checkout, rel_path),
        )
    )


def test_ac1_unused_import_and_typing_any_are_both_in_the_digest(
    tmp_path: Path,
) -> None:
    digest = _replay(tmp_path, "unused_import_and_any.py.txt", "src/pkg/answer.py")
    assert digest.infra_error is False, digest.digest_text
    assert digest.clean is False
    found = {(f.gate, f.code, f.line) for f in digest.findings}
    assert (EnumCodeGate.RUFF_CHECK, "F401", 5) in found
    assert (EnumCodeGate.ANY_TYPES, "ONEX-ANY", 9) in found
    assert "F401" in digest.digest_text
    assert "ONEX-ANY" in digest.digest_text


def test_ac1_positive_control_a_clean_answer_is_clean(tmp_path: Path) -> None:
    digest = _replay(tmp_path, "clean_answer.py.txt", "src/pkg/answer.py")
    assert digest.clean is True, digest.digest_text


def test_ac3_run_ea0b2d12_answer_is_refused_by_ruff_format(tmp_path: Path) -> None:
    digest = _replay(
        tmp_path,
        "run_ea0b2d12_model_unresolved_field.py.txt",
        "src/omnibase_core/models/lane_desired_state/model_unresolved_field.py",
    )
    assert digest.infra_error is False, digest.digest_text
    assert (EnumCodeGate.RUFF_FORMAT, "format") in {
        (f.gate, f.code) for f in digest.findings
    }


def test_ac3_run_f847f2cc_answer_has_the_two_unused_imports(tmp_path: Path) -> None:
    digest = _replay(
        tmp_path,
        "run_f847f2cc_model_lane_desired_state_identity.py.txt",
        "src/omnibase_core/models/lane_desired_state/model_lane_desired_state_identity.py",
    )
    assert digest.infra_error is False, digest.digest_text
    f401 = [f.message for f in digest.findings if f.code == "F401"]
    assert any("get_args" in m for m in f401), digest.digest_text
    assert any("get_origin" in m for m in f401), digest.digest_text
