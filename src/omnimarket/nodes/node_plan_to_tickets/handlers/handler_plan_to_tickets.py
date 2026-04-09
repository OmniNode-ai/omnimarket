"""HandlerPlanToTickets — plan parsing with dependency extraction and cycle detection."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field


class ModelPlanToTicketsRequest(BaseModel):
    """Request to parse a plan document into ticket entries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_content: str = Field(..., description="Markdown plan content.")
    dry_run: bool = Field(default=False)


class ModelTicketEntry(BaseModel):
    """A single parsed ticket entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str = Field(..., description="Sequential ID: P1, P2, ...")
    title: str = Field(..., description="Section heading text.")
    content: str = Field(default="", description="Body text of the section.")
    dependencies: list[str] = Field(default_factory=list)


class ModelPlanToTicketsResult(BaseModel):
    """Result of a plan parsing request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="parsed")
    structure_type: str = Field(default="")
    epic_title: str = Field(default="")
    entry_count: int = Field(default=0, ge=0)
    entries: list[ModelTicketEntry] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    dry_run: bool = Field(default=False)


_TASK_RE = re.compile(r"^## (Task \d+:.+)$", re.MULTILINE)
_PHASE_RE = re.compile(r"^## (Phase \d+:.+)$", re.MULTILINE)
_H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_DEP_RE = re.compile(r"[Dd]ependenc(?:y|ies):\s*(.+)")
_OMN_RE = re.compile(r"OMN-\d+")


def _parse_sections(
    content: str, pattern: re.Pattern[str]
) -> list[tuple[str, str, int]]:
    """Return list of (heading, body, match_start) for each section."""
    matches = list(pattern.finditer(content))
    sections: list[tuple[str, str, int]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start:end].strip()
        sections.append((m.group(1).strip(), body, m.start()))
    return sections


def _extract_deps(body: str, id_map: dict[str, str], entry_id: str) -> list[str]:
    """Extract dependencies from a section body."""
    dep_match = _DEP_RE.search(body)
    if not dep_match:
        return []
    dep_text = dep_match.group(1)

    # Check for OMN-XXXX references first
    omn_refs = _OMN_RE.findall(dep_text)
    if omn_refs:
        return omn_refs

    # Otherwise map to P-IDs
    deps: list[str] = []
    for label, pid in id_map.items():
        if pid == entry_id:
            continue
        # Match "Task N" or "Phase N" prefix
        prefix = label.split(":")[0].strip()
        if re.search(rf"\b{re.escape(prefix)}\b", dep_text, re.IGNORECASE):
            deps.append(pid)
    return deps


def _detect_cycle(entries: list[ModelTicketEntry]) -> bool:
    """Return True if there is a dependency cycle among entries."""
    graph: dict[str, list[str]] = {e.entry_id: list(e.dependencies) for e in entries}
    visited: set[str] = set()
    in_stack: set[str] = set()

    def dfs(node: str) -> bool:
        if node not in graph:
            return False
        visited.add(node)
        in_stack.add(node)
        for neighbour in graph[node]:
            if neighbour not in graph:
                continue
            if neighbour not in visited:
                if dfs(neighbour):
                    return True
            elif neighbour in in_stack:
                return True
        in_stack.discard(node)
        return False

    return any(node not in visited and dfs(node) for node in list(graph))


class HandlerPlanToTickets:
    """Handler that parses a markdown plan into structured ticket entries."""

    def handle(self, request: ModelPlanToTicketsRequest) -> ModelPlanToTicketsResult:
        """Parse plan content and return structured entries."""
        content = request.plan_content

        # Detect epic title
        h1_match = _H1_RE.search(content)
        epic_title = h1_match.group(1).strip() if h1_match else ""

        # Try Task sections first, fall back to Phase sections
        task_sections = _parse_sections(content, _TASK_RE)
        phase_sections = _parse_sections(content, _PHASE_RE)

        if task_sections:
            sections = task_sections
            structure_type = "task_sections"
        elif phase_sections:
            sections = phase_sections
            structure_type = "phase_sections"
        else:
            return ModelPlanToTicketsResult(
                status="error",
                validation_errors=[
                    "No valid structure found (expected ## Task N: or ## Phase N: headings)"
                ],
                dry_run=request.dry_run,
            )

        # Build ID map: heading -> P-ID
        id_map: dict[str, str] = {}
        for i, (heading, _body, _pos) in enumerate(sections):
            id_map[heading] = f"P{i + 1}"

        # Validate content and build entries
        errors: list[str] = []
        entries: list[ModelTicketEntry] = []

        for heading, body, _pos in sections:
            pid = id_map[heading]
            if not body:
                errors.append(f"Entry {pid!r} ({heading!r}) has no content")
                continue
            deps = _extract_deps(body, id_map, pid)
            entries.append(
                ModelTicketEntry(
                    entry_id=pid,
                    title=heading,
                    content=body,
                    dependencies=deps,
                )
            )

        if errors:
            return ModelPlanToTicketsResult(
                status="error",
                validation_errors=errors,
                dry_run=request.dry_run,
            )

        # Cycle detection
        if _detect_cycle(entries):
            return ModelPlanToTicketsResult(
                status="error",
                validation_errors=["Circular dependency detected among entries"],
                entries=entries,
                dry_run=request.dry_run,
            )

        return ModelPlanToTicketsResult(
            status="parsed",
            structure_type=structure_type,
            epic_title=epic_title,
            entry_count=len(entries),
            entries=entries,
            dry_run=request.dry_run,
        )


__all__: list[str] = [
    "HandlerPlanToTickets",
    "ModelPlanToTicketsRequest",
    "ModelPlanToTicketsResult",
    "ModelTicketEntry",
]
