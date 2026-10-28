# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the diagram generator and the diagram-drift check: text in, text and issues out."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

EnumDiagramView = Literal["full", "collapsed"]
EnumDiagramKind = Literal["state_machine", "graph"]
EnumDiagramIssueKind = Literal[
    "diagram_differs",
    "diagram_without_fragment",
    "fragment_without_diagram",
    "fragment_refused",
    "duplicate_fragment",
    "missing_contract_line",
    "malformed_marker",
    "view_not_supported",
]


class ModelRenderContractDiagramRequest(BaseModel):
    """A contract fragment's text and the view to draw; nothing else is an input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["render_contract_diagram"] = "render_contract_diagram"
    fragment_text: str
    view: EnumDiagramView = "full"


class ModelRenderContractDiagramResult(BaseModel):
    """The Mermaid text generated from the fragment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumDiagramKind
    view: EnumDiagramView
    mermaid: str


class ModelCheckDocumentDiagramsRequest(BaseModel):
    """A document's text; the markers inside it name the contracts and views."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["check_document_diagrams"] = "check_document_diagrams"
    document_text: str


class ModelDiagramIssue(BaseModel):
    """One reason the document's diagrams do not match its fragments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumDiagramIssueKind
    contract_id: str
    document_line: int = Field(
        ge=0, description="1-based line in the document; 0 when none"
    )
    diagram_line: int = Field(
        default=0,
        ge=0,
        description="1-based line inside the diagram block; 0 when none",
    )
    message: str


class ModelCheckDocumentDiagramsResult(BaseModel):
    """Every issue found and the exit code a caller returns (0 clean, 1 any issue)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issues: tuple[ModelDiagramIssue, ...]
    diagrams_checked: int = Field(ge=0)
    fragments_found: int = Field(ge=0)
    exit_code: int = Field(ge=0, le=1)


class ModelLoadedFragment(BaseModel):
    """A fragment the core model accepted, kept as the raw mapping the file holds.

    The graph model keeps only ``node_id``, ``node_type`` and ``depends_on`` of a node and drops
    ``description`` and ``step_config``; the generator draws the descriptions, so it reads the
    raw mapping beside the structural validation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumDiagramKind
    initial_state: str = ""
    terminal_states: tuple[str, ...] = ()
    transitions: tuple[dict[str, JsonValue], ...] = ()
    nodes: tuple[dict[str, JsonValue], ...] = ()
