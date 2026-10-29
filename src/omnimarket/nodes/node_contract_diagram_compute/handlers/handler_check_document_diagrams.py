# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``check_document_diagrams``: every Mermaid diagram of a document equals the one its fragment generates.

A ``<!-- diagram-contract: <node>:<block> [collapsed] -->`` marker precedes a ``mermaid`` block and
pairs by id with the ``<!-- contract-fragment: <node>:<block> -->`` marker that precedes a ``yaml``
block. The first non-blank line under a diagram must name the node of its contract.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_contract_diagram_compute.handlers.handler_render_contract_diagram import (
    load_fragment,
    render_loaded_fragment,
)
from omnimarket.nodes.node_contract_diagram_compute.models.model_contract_diagram import (
    EnumDiagramIssueKind,
    EnumDiagramView,
    ModelCheckDocumentDiagramsRequest,
    ModelCheckDocumentDiagramsResult,
    ModelDiagramIssue,
    ModelLoadedFragment,
)

_DIAGRAM_MARKER = re.compile(
    r"^<!-- diagram-contract: ([^:\s]+):(\S+?)(?: (collapsed))? -->$"
)
_FRAGMENT_MARKER = re.compile(r"^<!-- contract-fragment: ([^:\s]+):(\S+?) -->$")
_FENCE = "```"


class _Issues:
    """The issues found so far; ``add`` appends one."""

    def __init__(self) -> None:
        self.found: list[ModelDiagramIssue] = []

    def add(
        self,
        kind: EnumDiagramIssueKind,
        contract_id: str,
        document_line: int,
        message: str,
        diagram_line: int = 0,
    ) -> None:
        self.found.append(
            ModelDiagramIssue(
                kind=kind,
                contract_id=contract_id,
                document_line=document_line,
                diagram_line=diagram_line,
                message=message,
            )
        )


class _Block:
    def __init__(
        self, language: str, body: list[str], fence_index: int, end_index: int
    ):
        self.language = language
        self.body = body
        self.fence_index = fence_index
        self.end_index = end_index


def _read_block(lines: list[str], marker_index: int) -> _Block | None:
    """The fenced block after a marker, skipping blank lines; ``None`` when there is none."""
    index = marker_index + 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or not lines[index].startswith(_FENCE):
        return None
    language = lines[index][len(_FENCE) :].strip()
    end = index + 1
    while end < len(lines) and not lines[end].startswith(_FENCE):
        end += 1
    if end >= len(lines):
        return None
    return _Block(language, lines[index + 1 : end], index, end)


class HandlerCheckDocumentDiagrams:
    """COMPUTE handler: a document's text in, issues and an exit code out."""

    def handle(
        self, request: ModelCheckDocumentDiagramsRequest
    ) -> ModelCheckDocumentDiagramsResult:
        lines = request.document_text.splitlines()
        sink = _Issues()
        add = sink.add

        fragments: dict[str, tuple[int, _Block]] = {}
        diagrams: list[tuple[str, str, str, EnumDiagramView, int, _Block]] = []
        for index, raw in enumerate(lines):
            marker = raw.strip()
            fragment_match = _FRAGMENT_MARKER.match(marker)
            diagram_match = _DIAGRAM_MARKER.match(marker)
            if not fragment_match and not diagram_match:
                continue
            block = _read_block(lines, index)
            if fragment_match:
                contract_id = f"{fragment_match[1]}:{fragment_match[2]}"
                if block is None or block.language != "yaml":
                    add(
                        "malformed_marker",
                        contract_id,
                        index + 1,
                        "a contract-fragment marker is followed by no yaml block",
                    )
                elif contract_id in fragments:
                    add(
                        "duplicate_fragment",
                        contract_id,
                        index + 1,
                        f"a second fragment for {contract_id}; the first is at line {fragments[contract_id][0]}",
                    )
                else:
                    fragments[contract_id] = (index + 1, block)
            elif diagram_match:
                contract_id = f"{diagram_match[1]}:{diagram_match[2]}"
                if block is None or block.language != "mermaid":
                    add(
                        "malformed_marker",
                        contract_id,
                        index + 1,
                        "a diagram-contract marker is followed by no mermaid block",
                    )
                else:
                    view: EnumDiagramView = "collapsed" if diagram_match[3] else "full"
                    diagrams.append(
                        (
                            contract_id,
                            diagram_match[1],
                            diagram_match[2],
                            view,
                            index + 1,
                            block,
                        )
                    )

        loaded: dict[str, ModelLoadedFragment | None] = {}
        for contract_id, (line, block) in fragments.items():
            try:
                loaded[contract_id] = load_fragment("\n".join(block.body) + "\n")
            except ValueError as refused:
                loaded[contract_id] = None
                add(
                    "fragment_refused",
                    contract_id,
                    line,
                    f"the core model refused the fragment: {refused}",
                )

        drawn: set[str] = set()
        for contract_id, node, block_name, view, marker_line, block in diagrams:
            drawn.add(contract_id)
            self._check_caption(lines, node, contract_id, block, sink)
            if contract_id not in fragments:
                add(
                    "diagram_without_fragment",
                    contract_id,
                    marker_line,
                    f"no contract-fragment marker names {contract_id}",
                )
                continue
            fragment = loaded[contract_id]
            if fragment is None:
                continue
            if fragment.kind != block_name:
                add(
                    "fragment_refused",
                    contract_id,
                    fragments[contract_id][0],
                    f"the marker names block {block_name} but the fragment holds a {fragment.kind}",
                )
                continue
            if fragment.kind == "state_machine" and view == "collapsed":
                add(
                    "view_not_supported",
                    contract_id,
                    marker_line,
                    "a state machine has only the full view",
                )
                continue
            self._compare(
                contract_id, render_loaded_fragment(fragment, view), block, sink
            )

        for contract_id, (line, _) in fragments.items():
            if contract_id not in drawn:
                add(
                    "fragment_without_diagram",
                    contract_id,
                    line,
                    f"no diagram-contract marker names {contract_id}",
                )

        issues = sorted(sink.found, key=lambda issue: issue.document_line)
        return ModelCheckDocumentDiagramsResult(
            issues=tuple(issues),
            diagrams_checked=len(diagrams),
            fragments_found=len(fragments),
            exit_code=1 if issues else 0,
        )

    @staticmethod
    def _check_caption(
        lines: list[str], node: str, contract_id: str, block: _Block, sink: _Issues
    ) -> None:
        index = block.end_index + 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines) or node not in lines[index]:
            sink.add(
                "missing_contract_line",
                contract_id,
                block.end_index + 1,
                f"the line under the diagram does not name {node}",
            )

    @staticmethod
    def _compare(
        contract_id: str, generated: str, block: _Block, sink: _Issues
    ) -> None:
        expected = generated.splitlines()
        found = [line.rstrip() for line in block.body]
        for position in range(max(len(expected), len(found))):
            want = expected[position] if position < len(expected) else None
            have = found[position] if position < len(found) else None
            if want == have:
                continue
            number = position + 1
            sink.add(
                "diagram_differs",
                contract_id,
                block.fence_index + 1 + number,
                f"line {number} of the diagram differs from the one generated from the fragment: "
                f"expected {want.strip() if want is not None else '(no line)'!r}, "
                f"found {have.strip() if have is not None else '(no line)'!r}",
                number,
            )
            return


__all__ = ["HandlerCheckDocumentDiagrams"]
