# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCodeGateDigest — a delegated file's lint and type gate findings, as
one bounded digest (OMN-19527).

Pure definition-B compute: ``handle(request: ModelCodeGateDigestRequest) ->
ModelCodeGateDigest``. No I/O, no envelope type.

WHY. In the delegation capability matrix of 2026-09-25, three code answers
whose behaviour was right failed only on a repository gate the prompt never
named (an unused import, two lines over 88, a ``typing.Any``). Two repair runs
passed every DoD item once the exact linter output was pasted into the prompt.
This compute is that paste, made mechanical: the tools' own words, per finding,
restricted to the delegated file, capped, and fingerprinted.

RULES.

* ``ruff check``, ``ruff format --check`` and ``mypy --strict`` exit 0 when
  clean and 1 when they have findings. Any other exit code, or no exit code,
  means the tool itself failed (not installed, bad config, crash): that is
  ``infra_error``, never clean and never a finding to repair.
* Exit 1 with nothing parsed is a finding (``unparsed``) carrying the output's
  tail, never clean: a format change in a tool must not read as a pass.
* Findings in other files are dropped: mypy may report on an imported module,
  and those are not the delegated file's to fix.
* ``typing.Any`` is found here from the source's syntax tree, because ONEX
  refuses it (``onex-validate-any-types``) and neither tool does.

The parsers began as a delegated answer (``onex delegate``, run f69ca99f,
Qwen3.8-27B); two defects were fixed by hand: ruff codes with a multi-letter
prefix (``RUF100``) did not match, and a use of ``Any`` above its import line
was missed.
"""

from __future__ import annotations

import ast
import hashlib
import re
from typing import Final

from omnimarket.nodes.node_code_gate_digest_compute.models.model_code_gate_digest import (
    MAX_DIGEST_CHARS,
    MAX_FINDING_MESSAGE_CHARS,
    MAX_FINDINGS,
    TOOL_GATES,
    EnumCodeGate,
    ModelCodeGateDigest,
    ModelCodeGateDigestRequest,
    ModelGateFinding,
    ModelGateToolOutput,
)

#: ``path:line:col: CODE [*] message`` (``ruff check --output-format=concise``).
_RUFF_CONCISE_RE: Final = re.compile(
    r"^(?P<path>[^:\n]+):(?P<line>\d+):(?P<col>\d+): "
    r"(?P<code>[A-Z]+[0-9]+) (?:\[\*\] )?(?P<message>.*)$"
)
#: ``path:line: error: message  [code]`` (mypy; notes are not findings).
_MYPY_ERROR_RE: Final = re.compile(
    r"^(?P<path>[^:\n]+):(?P<line>\d+)(?::\d+)?: error: "
    r"(?P<message>.+?)(?:\s+\[(?P<code>[a-z0-9-]+)\])?$"
)
#: Exit codes meaning "ran, and has findings" for all three tools.
_EXIT_CLEAN, _EXIT_FINDINGS = 0, 1
_ANY_CODE = "ONEX-ANY"
_ANY_MESSAGE = (
    "typing.Any is refused (onex-validate-any-types); use a precise type, "
    "a Protocol, a TypeVar or object"
)
_TAIL_CHARS = 300


def parse_ruff_concise(output: str) -> list[tuple[str, int, str, str]]:
    """(path, line, code, message) per finding line of ruff's concise output."""
    found: list[tuple[str, int, str, str]] = []
    for raw in output.splitlines():
        match = _RUFF_CONCISE_RE.match(raw.strip())
        if match:
            found.append(
                (
                    match.group("path"),
                    int(match.group("line")),
                    match.group("code"),
                    match.group("message").strip(),
                )
            )
    return found


def parse_mypy(output: str) -> list[tuple[str, int, str, str]]:
    """(path, line, code, message) per ``error:`` line; notes and summary skipped."""
    found: list[tuple[str, int, str, str]] = []
    for raw in output.splitlines():
        match = _MYPY_ERROR_RE.match(raw.strip())
        if match:
            found.append(
                (
                    match.group("path"),
                    int(match.group("line")),
                    match.group("code") or "mypy",
                    match.group("message").strip(),
                )
            )
    return found


def find_any_usages(source: str) -> list[int]:
    """Sorted line numbers where ``typing.Any`` is used; [] if it does not parse."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    any_names: set[str] = set()
    typing_names: set[str] = set()
    # Imports first, in a pass of their own, so a use above its import counts.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            any_names.update(a.asname or a.name for a in node.names if a.name == "Any")
        elif isinstance(node, ast.Import):
            typing_names.update(
                a.asname or a.name for a in node.names if a.name == "typing"
            )
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and node.id in any_names) or (
            isinstance(node, ast.Attribute)
            and node.attr == "Any"
            and isinstance(node.value, ast.Name)
            and node.value.id in typing_names
        ):
            lines.add(node.lineno)
    return sorted(lines)


def _same_file(reported: str, path: str) -> bool:
    reported = reported.strip().removeprefix("./")
    return (
        reported == path
        or path.endswith("/" + reported)
        or reported.endswith("/" + path)
    )


def _tail(text: str) -> str:
    text = " ".join(text.split())
    return text[-_TAIL_CHARS:]


def _finding(
    gate: EnumCodeGate, line: int, code: str, message: str
) -> ModelGateFinding:
    return ModelGateFinding(
        gate=gate,
        line=line,
        code=code[:64],
        message=message[:MAX_FINDING_MESSAGE_CHARS],
    )


def _tool_findings(
    output: ModelGateToolOutput, path: str
) -> tuple[list[ModelGateFinding], str | None]:
    """The tool's findings on ``path``, or an infra fault description."""
    if output.exit_code == _EXIT_CLEAN:
        return [], None
    if output.exit_code != _EXIT_FINDINGS:
        return (
            [],
            f"{output.gate.value} exited {output.exit_code}: {_tail(output.output)}",
        )
    gate = output.gate
    if gate is EnumCodeGate.RUFF_FORMAT:
        return [
            _finding(
                gate,
                0,
                "format",
                "ruff format would reformat this file (run `ruff format` on it; "
                "lines over the repository's line length are the usual cause)",
            )
        ], None
    parsed = (
        parse_ruff_concise(output.output)
        if gate is EnumCodeGate.RUFF_CHECK
        else parse_mypy(output.output)
    )
    if not parsed:
        return [_finding(gate, 0, "unparsed", _tail(output.output))], None
    return [
        _finding(gate, line, code, message)
        for reported, line, code, message in parsed
        if _same_file(reported, path)
    ], None


def _render(
    path: str,
    shown: list[ModelGateFinding],
    total: int,
    faults: list[str],
) -> str:
    # Infra faults first (each capped by _tail), then whole finding lines while
    # they fit; the trailer always says how many findings the text left out.
    trailer_room = 60
    lines = [f"{path}: [infra] {fault}" for fault in faults]
    used = sum(len(line) + 1 for line in lines)
    rendered = 0
    for f in shown:
        line = f"{path}:{f.line}: [{f.gate.value}] {f.code} {f.message}"
        if used + len(line) + 1 > MAX_DIGEST_CHARS - trailer_room:
            break
        lines.append(line)
        used += len(line) + 1
        rendered += 1
    if total > rendered:
        lines.append(f"... {total - rendered} more finding(s) not shown")
    return "\n".join(lines)[:MAX_DIGEST_CHARS]


def digest_code_gates(request: ModelCodeGateDigestRequest) -> ModelCodeGateDigest:
    findings: list[ModelGateFinding] = []
    faults: list[str] = []
    by_gate = {output.gate: output for output in request.outputs}
    for gate in TOOL_GATES:
        output = by_gate.get(gate)
        if output is None:
            faults.append(f"{gate.value} did not run")
            continue
        tool_findings, fault = _tool_findings(output, request.path)
        findings.extend(tool_findings)
        if fault:
            faults.append(fault)
    findings.extend(
        _finding(EnumCodeGate.ANY_TYPES, line, _ANY_CODE, _ANY_MESSAGE)
        for line in find_any_usages(request.source)
    )
    findings.sort(key=lambda f: (f.line, f.gate.value, f.code, f.message))
    shown = findings[:MAX_FINDINGS]
    clean = not findings and not faults
    fingerprint = ""
    if not clean:
        keys = sorted({f"{f.gate.value}\0{f.code}\0{f.message}" for f in findings})
        keys += sorted(f"infra\0{fault.split(':', 1)[0]}" for fault in faults)
        fingerprint = hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()
    return ModelCodeGateDigest(
        path=request.path,
        clean=clean,
        infra_error=bool(faults),
        findings=tuple(shown),
        finding_count=len(findings),
        truncated=len(findings) > len(shown),
        gates_run=(*(g for g in TOOL_GATES if g in by_gate), EnumCodeGate.ANY_TYPES),
        digest_text=_render(request.path, shown, len(findings), faults),
        fingerprint=fingerprint,
    )


class HandlerCodeGateDigest:
    """COMPUTE handler: a delegated file's gate outputs in, one digest out."""

    def handle(self, request: ModelCodeGateDigestRequest) -> ModelCodeGateDigest:
        return digest_code_gates(request)


__all__ = [
    "HandlerCodeGateDigest",
    "digest_code_gates",
    "find_any_usages",
    "parse_mypy",
    "parse_ruff_concise",
]
