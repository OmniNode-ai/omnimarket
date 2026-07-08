#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Offline self-test of the SWE-discriminator grader (OMN-13988, zero model cost).

Proves the deterministic hard floor is a real discriminator BEFORE any live
model call: the actual merged-PR solution must PASS the floor and the pre-fix
source must FAIL it. If either fails, the harness is miscalibrated and a live
run would be meaningless.
"""

from __future__ import annotations

import sys

from omnimarket.delegation.swe_discriminator.corpus import load_corpus
from omnimarket.delegation.swe_discriminator.grader import grade_floor

# The REAL merged-PR solutions (post-fix), pasted as fenced blocks — the grader
# must PASS these. Held here (not in the corpus) because the corpus never serves
# a solution to an arm.
POSTFIX = {
    "OMN-13964-create-ticket-field": '''```python
class ModelCreateTicketRequest(BaseModel):
    """Request to create a Linear ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(..., description="Ticket title.")
    description: str = Field(default="", description="Optional description.")
    repo: str | None = Field(default=None, description="Primary repo for scoping.")
    parent: str | None = Field(default=None, description="Parent ticket ID (OMN-XXXX).")
    blocked_by: list[str] = Field(default_factory=list, description="Blocking ticket IDs.")
    dry_run: bool = Field(default=False)
    team: str = Field(default="Omninode")
    allow_arch_violation: bool = Field(
        default=False,
        description="Bypass architecture dependency validation (contract input).",
    )


class ModelCreateTicketResult(BaseModel):
    """Result of a ticket creation request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(default="created")
    ticket_id: str = Field(default="")
    ticket_url: str = Field(default="")
    title: str = Field(default="")
    team: str = Field(default="Omninode")
    is_seam_ticket: bool = Field(default=False)
    interfaces_touched: list[str] = Field(default_factory=list)
    contract_completeness: str = Field(default="stub")
    validation_errors: list[str] = Field(default_factory=list)
    description_body: str = Field(default="")
    dry_run: bool = Field(default=False)
```''',
    "OMN-13816-state-coverage-ast": """```python
def _references_runtime(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, (ast.Name, ast.Attribute, ast.Call, ast.Subscript, ast.Await, ast.Starred)):
            return True
    return False


def _is_vacuous_compare(node: ast.Compare) -> bool:
    operands = [node.left, *node.comparators]
    dumps = [ast.dump(op) for op in operands]
    if len(dumps) >= 2 and all(d == dumps[0] for d in dumps[1:]):
        return True
    return not any(_references_runtime(op) for op in operands)


def _vacuous_occurrence_ids(tree: ast.AST) -> set[int]:
    excluded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            excluded.add(id(node.value))
        elif isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant):
            excluded.add(id(node.test))
        elif isinstance(node, ast.Compare) and _is_vacuous_compare(node):
            for child in ast.walk(node):
                excluded.add(id(child))
    return excluded


def _state_match_nodes(tree: ast.AST, state: str, *, identifier_state: bool) -> list[ast.AST]:
    matches: list[ast.AST] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and node.value == state) or (
            identifier_state and isinstance(node, ast.Attribute) and node.attr == state
        ):
            matches.append(node)
    return matches


def _state_covered(state: str, test_contents: str | list[str]) -> bool:
    if not state:
        return False
    sources = [test_contents] if isinstance(test_contents, str) else list(test_contents)
    identifier_state = bool(_IDENTIFIER_RE.match(state)) and "." not in state
    for source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        excluded = _vacuous_occurrence_ids(tree)
        matches = _state_match_nodes(tree, state, identifier_state=identifier_state)
        if any(id(match) not in excluded for match in matches):
            return True
    return False
```""",
}


def main() -> int:
    tasks = load_corpus()
    ok = True
    for task in tasks:
        # 1. post-fix (real merged solution) MUST pass the floor
        passed, detail = grade_floor(task, POSTFIX[task.task_id])
        print(
            f"[{task.task_id}] post-fix floor: {'PASS' if passed else 'FAIL'} :: {detail}"
        )
        if not passed:
            ok = False
            print("  !! merged solution failed the floor — harness miscalibrated")
        # 2. pre-fix (served context) MUST fail the floor (else task is vacuous)
        prefix_artifact = f"```python\n{task.context_code}\n```"
        p2, d2 = grade_floor(task, prefix_artifact)
        print(f"[{task.task_id}] pre-fix  floor: {'PASS' if p2 else 'FAIL'} :: {d2}")
        if p2:
            ok = False
            print("  !! pre-fix code already passed — task does not discriminate")
    print(f"\nSELF-TEST: {'OK — grader discriminates' if ok else 'BROKEN'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
