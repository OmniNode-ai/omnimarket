# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Execute the source row's HTTP readback as a bounded typed operation.

The phase selects a source row, not a command or URL. Only a read-only curl GET
and an optional JSON field read/equality predicate are understood. Shells,
redirects, arbitrary jq programs and mutations never run. Unrecognized probes
remain explicit refusals; merge state is never substituted for runtime proof.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import socket
import time
from datetime import UTC, datetime
from typing import TypedDict, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, JsonValue

from ..models.model_morning_runtime_probe import (
    ModelMorningRuntimeProbeCommand,
    ModelMorningRuntimeProbeRequest,
    ModelMorningRuntimeProbeResult,
)
from ..tools.base import MorningTool, MorningToolRefusalError, Result
from ..tools.context import ModelMorningToolContext
from ..tools.local_reads import read_source_document

MAX_RESPONSE_BYTES = 32_000
PROBE_TIMEOUT_SECONDS = 10
_JSON_FIELD = r"\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"


class _SourceFacts(TypedDict):
    doc: str
    source_line: int
    source_copy: str
    source_sha256: str
    row_sha256: str
    source_command: str
    done_proof: str
    host: str
    observed_at: str


def _cells(line: str) -> list[str]:
    """Table separators outside inline code, including a pipeline in a cell."""
    return [
        cell.strip().replace("\\|", "|")
        for cell in re.split(
            r"(?<!\\)\|(?=(?:[^`]*`[^`]*`)*[^`]*$)", line.strip().strip("|")
        )
    ]


def _row_probe(text: str, source_line: int) -> tuple[str, str, str]:
    lines = text.splitlines()
    index = source_line - 1
    if index >= len(lines) or not lines[index].lstrip().startswith("|"):
        raise MorningToolRefusalError("source_line is not a source table row")
    header = index - 1
    while header >= 0 and lines[header].lstrip().startswith("|"):
        header -= 1
    headings = _cells(lines[header + 1])
    columns = [
        n
        for n, value in enumerate(headings)
        if re.fullmatch(r"done[ -]proof(?: probe)?", value, re.I)
    ]
    cells = _cells(lines[index])
    if len(columns) != 1 or len(cells) != len(headings) or index <= header + 2:
        raise MorningToolRefusalError("row has no unambiguous Done-proof column")
    proof = cells[columns[0]]
    commands = re.findall(r"(?<!`)`([^`\n]+)`(?!`)", proof)
    if len(commands) != 1:
        raise MorningToolRefusalError(
            "Done-proof must contain exactly one supported readback; no partial probe"
        )
    return lines[index], proof, commands[0]


def _dev_hosts(ctx: ModelMorningToolContext) -> set[str]:
    """Use the existing declared dev target, never an ambient URL or broker."""
    from omnibase_infra.cli.delegate_lane import (
        DelegateLaneSelectionError,
        resolve_lane_selection_at,
    )

    try:
        target = resolve_lane_selection_at("dev", ctx.root)
    except DelegateLaneSelectionError as exc:
        raise MorningToolRefusalError(
            f"dev target declaration is unreadable: {exc}"
        ) from None
    hosts = {
        urlsplit("//" + broker.strip()).hostname or ""
        for broker in target.bootstrap_servers.split(",")
    } - {""}
    # Resolve only declared names: a source row may spell the same host by IP.
    addresses = set(hosts)
    for host in hosts:
        try:
            addresses.update(str(item[4][0]) for item in socket.getaddrinfo(host, None))
        except OSError:
            continue
    return addresses


def _compile(command: str) -> tuple[str, str | None, JsonValue, bool]:
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise MorningToolRefusalError(f"invalid readback: {exc}") from None
    if not words or words.pop(0) != "curl":
        raise MorningToolRefusalError(
            "unsupported probe: requires a read-only curl GET"
        )
    while words and words[0].startswith("-"):
        flag = words.pop(0)
        if flag not in ("--fail", "--silent", "--show-error") and not re.fullmatch(
            r"-[fsS]+", flag
        ):
            raise MorningToolRefusalError(f"unsupported curl option {flag!r}")
    if not words:
        raise MorningToolRefusalError("readback has no target")
    url = words.pop(0)
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(c in url for c in "\r\n\\`$<>{};&|")
    ):
        raise MorningToolRefusalError("invalid HTTP readback target")
    if not words:
        return url, None, None, False
    if len(words) != 4 or words[:2] != ["|", "jq"] or words[2] not in ("-e", "-r"):
        raise MorningToolRefusalError("unsupported pipeline; no shell is executed")
    expression = words[3]
    if words[2] == "-r" and re.fullmatch(_JSON_FIELD, expression):
        return url, expression, None, False
    match = re.fullmatch(rf"({_JSON_FIELD})\s*==\s*(.+)", expression)
    if words[2] != "-e" or match is None:
        raise MorningToolRefusalError(
            "unsupported JSON predicate; it must test a field"
        )
    try:
        expected = json.loads(match[2])
    except ValueError:
        raise MorningToolRefusalError(
            "JSON equality requires a literal value"
        ) from None
    if isinstance(expected, (dict, list)):
        raise MorningToolRefusalError("JSON equality requires a scalar literal")
    return url, match[1], expected, True


class HandlerMorningRuntimeProbe:
    def handle(
        self, request: ModelMorningRuntimeProbeCommand
    ) -> ModelMorningRuntimeProbeResult:
        context = request.context
        resolution, text = read_source_document(context, request.doc)
        if text is None:
            raise MorningToolRefusalError(
                "source document is unreadable; probe is UNKNOWN"
            )
        source_hash = hashlib.sha256(text.encode()).hexdigest()
        if request.source_sha256 != source_hash:
            raise MorningToolRefusalError(
                "source changed since it was read; read the source again"
            )
        row, proof, command = _row_probe(text, request.source_line)
        if re.search(r"\bprod(?:uction)?\b", row, re.I):
            raise MorningToolRefusalError(
                "production rows are outside the dev probe scope"
            )
        url, field, expected, assertion = _compile(command)
        if urlsplit(url).hostname not in _dev_hosts(context):
            raise MorningToolRefusalError(
                "readback target is not the declared dev runtime host"
            )
        facts: _SourceFacts = {
            "doc": request.doc,
            "source_line": request.source_line,
            "source_copy": str(resolution["copy_read"]),
            "source_sha256": source_hash,
            "row_sha256": hashlib.sha256(row.encode()).hexdigest(),
            "source_command": command,
            "done_proof": proof,
            "host": socket.gethostname(),
            "observed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
            with (
                httpx.Client(
                    timeout=PROBE_TIMEOUT_SECONDS,
                    follow_redirects=False,
                    trust_env=False,
                    headers={"Accept-Encoding": "identity"},
                ) as client,
                client.stream("GET", url) as response,
            ):
                body = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise ValueError(
                            "readback exceeded its total time budget; UNKNOWN"
                        )
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        return ModelMorningRuntimeProbeResult(
                            **facts,
                            executed=True,
                            exit=None,
                            stdout="",
                            stderr="readback exceeds the evidence limit; UNKNOWN",
                            truncated=True,
                        )
                stdout = body.decode("utf-8", errors="replace")
                if not 200 <= response.status_code < 300:
                    return ModelMorningRuntimeProbeResult(
                        **facts,
                        executed=True,
                        exit=22,
                        stdout=stdout,
                        stderr=f"HTTP {response.status_code}; readback failed",
                    )
            if not stdout.strip():
                raise ValueError("empty readback; no positive runtime evidence")
            passed = None
            if field is not None:
                value = json.loads(stdout)
                for key in field[1:].split("."):
                    if not isinstance(value, dict) or key not in value:
                        raise ValueError(f"readback has no field {field}")
                    value = value[key]
                if assertion:
                    # JSON booleans are distinct from numbers (Python True == 1).
                    passed = type(value) is type(expected) and value == expected
                    stdout = "true\n" if passed else "false\n"
                else:
                    stdout = (
                        value if isinstance(value, str) else json.dumps(value)
                    ) + "\n"
            return ModelMorningRuntimeProbeResult(
                **facts,
                executed=True,
                exit=1 if passed is False else 0,
                stdout=stdout,
                stderr="",
                assertion_passed=passed,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ModelMorningRuntimeProbeResult(
                **facts,
                executed=True,
                exit=None,
                stdout="",
                stderr=str(exc),
            )


def runtime_probe(ctx: ModelMorningToolContext, params: BaseModel) -> Result:
    result = HandlerMorningRuntimeProbe().handle(
        ModelMorningRuntimeProbeCommand(
            context=ctx, **cast(ModelMorningRuntimeProbeRequest, params).model_dump()
        )
    )
    return cast(Result, result.model_dump(mode="json"))


RUNTIME_PROBE_TOOL = MorningTool(
    "runtime_probe",
    "Execute the declared source row's HTTP Done-proof readback on the declared dev "
    "runtime host. Select a document key, source_sha256 and one-based table-row line. Returns "
    "fresh output, source hashes and execution context; never infers a ladder stage. "
    "Supports curl GET and JSON field/equality readbacks; refuses other commands.",
    ModelMorningRuntimeProbeRequest,
    "read",
    runtime_probe,
)
