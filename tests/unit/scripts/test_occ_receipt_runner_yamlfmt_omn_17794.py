# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17794 — the receipt runner must emit YAML no formatter can falsify.

The defect, measured not inferred
---------------------------------
``occ_receipt_runner._dump`` serialized every receipt with
``yaml.safe_dump(..., width=10_000)``. PyYAML renders a multi-line string in
that shape as a **single-quoted** scalar, in which a newline is written as a
blank line. ``yamlfmt`` v0.21.0 — which ``onex_change_control``'s own
``Pre-commit`` job runs over exactly these files — round-trips that shape
through go-yaml's internal line marker and writes the marker back as literal
text, **destroying the newline**.

Reproduced on OCC#8132 (the autobound companion for ``omnimarket#2281``), file
``drift/dod_receipts/OMN-17459/dod-occ-diff-derived-behavior-proof/
test_passes.supersede.2281.yaml``. Before yamlfmt the parsed
``replacement.probe_stdout`` was::

    '...............................              [100%]\\n31 passed in 13.14s'

after yamlfmt it was::

    '...............................              [100%] <marker> 31 passed in 13.14s'

A durable receipt's captured stdout is evidence. A formatter that can rewrite
its VALUE can falsify it, so the companion could neither be committed honestly
(applying the formatter fabricates text) nor left alone (``yamlfmt`` reports
``files were modified by this hook``, ``CI Summary`` fails closed behind it,
and the OMN-15214 companion gate holds the product PR BLOCKED).

The two shapes yamlfmt rewrites, both measured against the real binary
-----------------------------------------------------------------------
1. Any multi-line scalar that is **not** a block scalar (plain, single- or
   double-quoted-with-a-real-newline): the newline becomes the literal marker.
2. A literal block scalar carrying the **keep** chomping indicator ``|+``
   (which PyYAML reaches for when the value ends in more than one newline):
   yamlfmt strips the ``+``, silently deleting the trailing blank lines.

So neither "always block" nor "always quoted" is safe on its own. The runner
picks per value and proves the choice, and ``_dump`` fails closed if the bytes
it is about to write do not reload to the object it was handed.

Why the invariant is asserted structurally as well as against the binary
------------------------------------------------------------------------
``test_*_survives_real_yamlfmt`` skips when the binary is absent (the repo's
established pattern — see ``test_occ_autobind_contention_omn_15247.py``). A
skipped test proves nothing, so the style invariant is ALSO asserted with
``yaml.compose``, which exposes each scalar node's real style. That test can
never skip.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts" / "ci"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import occ_receipt_runner as runner  # noqa: E402

# Byte-mirror of onex_change_control/.yamlfmt (google/yamlfmt v0.21.0), the
# config the hosted Pre-commit job resolves for every file this runner writes.
_OCC_YAMLFMT_CONF = """\
formatter:
  retain_line_breaks: true
  max_line_length: 100
  indent: 2
  include_document_start: true
  pad_line_comments: 2
"""

# The literal go-yaml line marker. Its ABSENCE from a written receipt is the
# assertion, which is why the token appears in this file at all.
_YAMLFMT_LINE_MARKER = (
    "#magic___^_^___line"  # test-literal-ok: absence is the assertion
)

# The exact captured stdout OCC#8132 carried, verbatim from the pristine
# receipt at head c54ee2fef8 (yaml.safe_load of the file before any formatting).
_OCC_8132_PROBE_STDOUT = (
    "..............................."
    "                                          [100%]\n"
    "31 passed in 13.14s"
)

# Shapes a real captured stdout takes. Each one is a value a formatter must not
# be able to alter.
_HOSTILE_STDOUTS: dict[str, str] = {
    "occ_8132_verbatim": _OCC_8132_PROBE_STDOUT,
    "trailing_spaces_on_a_line": "line one with trailing space   \nline two\n",
    "blank_line_inside": "first\n\nthird\n",
    "trailing_blank_lines": "alpha\nbeta\n\n\n",
    "line_that_is_only_spaces": "alpha\n   \nbeta\n",
    "leading_space": "  indented first line\nsecond\n",
    "tabs": "col1\tcol2\nrow\tval\n",
    "crlf": "one\r\ntwo\r\n",
    "unicode": "héllo ✓\nworld\n",
    "single_trailing_newline": "alpha\nbeta\n",
    "no_trailing_newline": "alpha\nbeta",
    "pytest_shaped": (
        "=" * 30 + " test session starts " + "=" * 30 + "\n"
        "platform darwin -- Python 3.13.5, pytest-8.4.2\n"
        "\n"
        "collected 31 items\n"
        "\n"
        "..............................."
        "                                          [100%]\n"
        "\n"
        "31 passed in 13.14s\n"
    ),
}


def _receipt_body(stdout: str) -> dict[str, Any]:
    """A receipt body shaped exactly like the one OCC#8132 carries."""
    return {
        "schema_version": "1.0.0",
        "ticket_id": "OMN-17459",
        "evidence_item_id": "dod-occ-diff-derived-behavior-proof",
        "check_type": "test_passes",
        "status": "PASS",
        "check_value": (
            "uv run pytest tests/nodes/node_post_merge_knowledge_sync_orchestrator/"
            "test_post_merge_knowledge_sync_orchestrator.py "
            "tests/test_market_skill_smokes.py -q"
        ),
        "probe_command": (
            "uv run pytest tests/nodes/node_post_merge_knowledge_sync_orchestrator/"
            "test_post_merge_knowledge_sync_orchestrator.py "
            "tests/test_market_skill_smokes.py -q"
        ),
        "probe_stdout": stdout,
        "actual_output": (
            "PASS: declared check executed in the OmniNode-ai/omnimarket checkout at "
            "PR #2281 head; exit status 0. Run: "
            "https://github.com/OmniNode-ai/omnimarket/actions/runs/33776721189"
        ),
        "exit_code": 0,
        "duration_ms": 21134,
        "pr_number": 2281,
        "working_dir": None,
    }


def _supersession_body(stdout: str) -> dict[str, Any]:
    """The nested shape — the one that actually shipped broken on OCC#8132."""
    return {
        "schema_version": "1.0.0",
        "ticket_id": "OMN-17459",
        "evidence_item_id": "dod-occ-diff-derived-behavior-proof",
        "check_type": "test_passes",
        "supersedes": (
            "drift/dod_receipts/OMN-17459/dod-occ-diff-derived-behavior-proof/"
            "test_passes.yaml"
        ),
        "reason": (
            "The base receipt records status PENDING: the check was declared but not "
            "executed, because the minting producer runs in the effects runtime with "
            "no product-repo checkout (OMN-16859). This record rebinds the key to a "
            "receipt produced by executing the declared check for real in the product "
            "checkout at PR #2281 head."
        ),
        "superseder": "omnimarket-ci occ-receipt-runner",
        "created_at": "2026-09-03T16:08:45Z",
        "tombstone": False,
        "replacement": _receipt_body(stdout),
    }


def _multiline_scalar_styles(text: str) -> list[tuple[str, str | None]]:
    """Every scalar in ``text`` whose VALUE spans lines, with its real style.

    ``yaml.compose`` keeps the node styles ``yaml.safe_load`` throws away, so
    this reads what was actually written rather than what was intended.
    """
    found: list[tuple[str, str | None]] = []

    def walk(node: yaml.nodes.Node) -> None:
        if isinstance(node, yaml.nodes.ScalarNode):
            if node.tag == "tag:yaml.org,2002:str" and "\n" in node.value:
                found.append((node.value, node.style))
        elif isinstance(node, yaml.nodes.MappingNode):
            for key, value in node.value:
                walk(key)
                walk(value)
        elif isinstance(node, yaml.nodes.SequenceNode):
            for item in node.value:
                walk(item)

    walk(yaml.compose(text))
    return found


class TestDumpIsValueFaithful:
    """The hard requirement: the bytes reload to the object that was handed in."""

    @pytest.mark.unit
    @pytest.mark.parametrize("name", sorted(_HOSTILE_STDOUTS))
    def test_written_receipt_reloads_to_the_object_it_was_given(
        self, tmp_path: Path, name: str
    ) -> None:
        body = _supersession_body(_HOSTILE_STDOUTS[name])
        target = tmp_path / "test_passes.supersede.2281.yaml"
        runner._dump(target, body)
        assert yaml.safe_load(target.read_text(encoding="utf-8")) == body


class TestNoFormatterRewritableShapeIsWritten:
    """The structural invariant. Never skips, so it always carries weight."""

    @pytest.mark.unit
    @pytest.mark.parametrize("name", sorted(_HOSTILE_STDOUTS))
    def test_every_multiline_scalar_is_block_or_escaped(
        self, tmp_path: Path, name: str
    ) -> None:
        """A multi-line scalar must be ``|`` or ``"`` — never plain or ``'``.

        Plain and single-quoted multi-line scalars are the shape yamlfmt
        rewrites into the line marker. Double-quoted carries no real newline at
        all (it escapes them), so there is nothing for a line marker to replace.
        """
        target = tmp_path / "receipt.yaml"
        runner._dump(target, _supersession_body(_HOSTILE_STDOUTS[name]))
        text = target.read_text(encoding="utf-8")
        offenders = [
            (value[:40], style)
            for value, style in _multiline_scalar_styles(text)
            if style not in ("|", '"')
        ]
        assert not offenders, (
            "a multi-line scalar was written in a style yamlfmt rewrites into "
            f"the go-yaml line marker: {offenders}\n{text}"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("name", sorted(_HOSTILE_STDOUTS))
    def test_no_block_scalar_carries_the_keep_chomping_indicator(
        self, tmp_path: Path, name: str
    ) -> None:
        """``|+`` is not safe: yamlfmt strips the ``+`` and drops the blanks."""
        target = tmp_path / "receipt.yaml"
        runner._dump(target, _supersession_body(_HOSTILE_STDOUTS[name]))
        text = target.read_text(encoding="utf-8")
        bad = [line for line in text.splitlines() if line.rstrip().endswith("|+")]
        assert not bad, (
            "a keep-chomped block scalar was written; yamlfmt strips the '+' and "
            f"deletes the trailing blank lines: {bad}\n{text}"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("name", sorted(_HOSTILE_STDOUTS))
    def test_the_marker_is_never_written(self, tmp_path: Path, name: str) -> None:
        target = tmp_path / "receipt.yaml"
        runner._dump(target, _supersession_body(_HOSTILE_STDOUTS[name]))
        assert _YAMLFMT_LINE_MARKER not in target.read_text(encoding="utf-8")


class TestSurvivesRealYamlfmt:
    """Drive the REAL formatter, so the invariant cannot drift from it."""

    @staticmethod
    def _run_yamlfmt(tmp_path: Path, target: Path) -> None:
        yamlfmt = shutil.which("yamlfmt")
        if yamlfmt is None:
            pytest.skip("yamlfmt binary not available (installed in the CI gate)")
        conf = tmp_path / ".yamlfmt"
        conf.write_text(_OCC_YAMLFMT_CONF, encoding="utf-8")
        result = subprocess.run(
            [yamlfmt, "-conf", str(conf), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    @pytest.mark.unit
    @pytest.mark.parametrize("name", sorted(_HOSTILE_STDOUTS))
    def test_yamlfmt_cannot_change_the_parsed_value(
        self, tmp_path: Path, name: str
    ) -> None:
        """The evidence bar: the VALUE is identical after the formatter runs."""
        body = _supersession_body(_HOSTILE_STDOUTS[name])
        target = tmp_path / "test_passes.supersede.2281.yaml"
        runner._dump(target, body)
        self._run_yamlfmt(tmp_path, target)
        after = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert after == body, (
            "yamlfmt altered a receipt VALUE — the evidence artifact is no longer "
            f"what was captured.\nwrote: {body['replacement']['probe_stdout']!r}\n"
            f"after: {after['replacement']['probe_stdout']!r}"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("name", sorted(_HOSTILE_STDOUTS))
    def test_yamlfmt_makes_no_change_at_all(self, tmp_path: Path, name: str) -> None:
        """The CI bar: ``files were modified by this hook`` must not fire."""
        target = tmp_path / "test_passes.supersede.2281.yaml"
        runner._dump(target, _supersession_body(_HOSTILE_STDOUTS[name]))
        before = target.read_bytes()
        self._run_yamlfmt(tmp_path, target)
        assert target.read_bytes() == before, (
            "yamlfmt rewrote the receipt; the hosted Pre-commit job reports "
            "'files were modified by this hook' and CI Summary fails closed:\n"
            f"{before.decode()}\n---- after ----\n{target.read_text()}"
        )

    @pytest.mark.unit
    def test_the_exact_occ_8132_receipt_is_a_yamlfmt_fixpoint(
        self, tmp_path: Path
    ) -> None:
        """The live regression, byte-for-byte from the failing companion."""
        body = _supersession_body(_OCC_8132_PROBE_STDOUT)
        target = tmp_path / "test_passes.supersede.2281.yaml"
        runner._dump(target, body)
        before = target.read_bytes()
        self._run_yamlfmt(tmp_path, target)
        assert target.read_bytes() == before
        assert yaml.safe_load(target.read_text(encoding="utf-8")) == body


class TestDumpFailsClosed:
    """A dumper that cannot represent its input must raise, never write."""

    @pytest.mark.unit
    def test_a_body_that_does_not_round_trip_raises_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "receipt.yaml"
        monkeypatch.setattr(runner.yaml, "safe_load", lambda _text: {"not": "the body"})
        with pytest.raises(ValueError, match="round-trip"):
            runner._dump(target, _supersession_body("alpha\nbeta\n"))
        assert not target.exists()
