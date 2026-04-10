# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelTicketContract — the YAML contract persisted in the Linear ticket description.

This mirrors the schema defined in the ticket-work skill's prompt.md.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ModelContractQuestion(BaseModel):
    """A clarifying question in the contract."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default="")
    text: str = Field(default="")
    category: str = Field(default="architecture")
    required: bool = Field(default=True)
    answer: str | None = Field(default=None)
    answered_at: str | None = Field(default=None)


class ModelContractRequirement(BaseModel):
    """An implementation requirement in the contract."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default="")
    statement: str = Field(default="")
    rationale: str = Field(default="")
    acceptance: list[str] = Field(default_factory=list)


class ModelContractVerification(BaseModel):
    """A verification step in the contract."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default="")
    title: str = Field(default="")
    kind: str = Field(default="unit_tests")
    command: str = Field(default="")
    expected: str = Field(default="exit 0")
    blocking: bool = Field(default=True)
    status: str = Field(default="pending")
    evidence: str | None = Field(default=None)
    executed_at: str | None = Field(default=None)


class ModelContractGate(BaseModel):
    """A human or policy gate in the contract."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default="")
    title: str = Field(default="")
    kind: str = Field(default="human_approval")
    required: bool = Field(default=True)
    status: str = Field(default="pending")
    notes: str | None = Field(default=None)
    resolved_at: str | None = Field(default=None)


class ModelContractContext(BaseModel):
    """Research context populated during the research phase."""

    model_config = ConfigDict(extra="forbid")

    relevant_files: list[str] = Field(default_factory=list)
    patterns_found: list[str] = Field(default_factory=list)
    notes: str = Field(default="")


class ModelTicketContract(BaseModel):
    """The YAML contract persisted in the Linear ticket description."""

    model_config = ConfigDict(extra="forbid")

    # Identity (set at intake)
    ticket_id: str = Field(default="")
    title: str = Field(default="")
    repo: str = Field(default="")
    branch: str | None = Field(default=None)

    # State
    phase: str = Field(default="intake")
    created_at: str = Field(default="")
    updated_at: str = Field(default="")

    # Research
    context: ModelContractContext = Field(default_factory=ModelContractContext)

    # Questions
    questions: list[ModelContractQuestion] = Field(default_factory=list)

    # Spec
    requirements: list[ModelContractRequirement] = Field(default_factory=list)
    verification: list[ModelContractVerification] = Field(default_factory=list)
    gates: list[ModelContractGate] = Field(default_factory=list)

    # Completion
    commits: list[str] = Field(default_factory=list)
    pr_url: str | None = Field(default=None)
    hardening_tickets: list[str] = Field(default_factory=list)

    def is_questions_complete(self) -> bool:
        """All required questions have non-empty answers."""
        return all(q.answer and q.answer.strip() for q in self.questions if q.required)

    def is_spec_complete(self) -> bool:
        """At least one requirement with acceptance criteria."""
        if not self.requirements:
            return False
        return all(len(r.acceptance) > 0 for r in self.requirements)

    def is_verification_complete(self) -> bool:
        """All blocking verification passed or skipped."""
        return all(
            v.status in ("passed", "skipped") for v in self.verification if v.blocking
        )

    def is_gates_complete(self) -> bool:
        """All required gates approved."""
        return all(g.status == "approved" for g in self.gates if g.required)

    def is_done(self) -> bool:
        """Contract is complete."""
        return (
            self.phase == "done"
            and self.is_questions_complete()
            and self.is_spec_complete()
            and self.is_verification_complete()
            and self.is_gates_complete()
        )

    def to_yaml(self) -> str:
        """Serialize contract to YAML string."""
        data = self.model_dump(exclude_none=False)
        return yaml.dump(data, default_flow_style=False, sort_keys=False)

    def with_updated_at(self) -> ModelTicketContract:
        """Return a copy with updated_at set to now."""
        return self.model_copy(
            update={"updated_at": datetime.utcnow().isoformat() + "Z"}
        )


def extract_contract(description: str) -> ModelTicketContract | None:
    """Extract and parse the contract YAML block from a ticket description.

    Looks for the last ## Contract section with a fenced yaml block.
    Returns None if not found or if YAML parsing fails.
    """
    marker = "## Contract"
    if marker not in description:
        return None

    idx = description.rfind(marker)
    contract_section = description[idx:]

    match = re.search(
        r"```(?:yaml|YAML)?\s*\n(.*?)\n\s*```", contract_section, re.DOTALL
    )
    if not match:
        return None

    try:
        raw: Any = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None

    if not isinstance(raw, dict):
        return None

    try:
        return ModelTicketContract.model_validate(raw)
    except Exception:
        return None


def update_description_with_contract(
    description: str, contract: ModelTicketContract
) -> str:
    """Update (or append) the contract YAML block in a ticket description."""
    marker = "## Contract"
    contract_yaml = contract.to_yaml()
    contract_block = f"\n---\n{marker}\n\n```yaml\n{contract_yaml}```\n"

    if marker in description:
        idx = description.rfind(marker)
        delimiter_match = re.search(r"\n---\n\s*$", description[:idx])
        if delimiter_match:
            return description[: delimiter_match.start()] + contract_block
        return description[:idx] + contract_block

    return description.rstrip() + contract_block


def persist_contract_locally(ticket_id: str, contract: ModelTicketContract) -> None:
    """Persist contract to local filesystem for hook injection.

    Path: ~/.claude/tickets/{ticket_id}/contract.yaml
    """
    import os
    from pathlib import Path

    tickets_dir = Path.home() / ".claude" / "tickets" / ticket_id
    tickets_dir.mkdir(parents=True, exist_ok=True)

    contract_path = tickets_dir / "contract.yaml"
    tmp_path = contract_path.with_suffix(".yaml.tmp")
    tmp_path.write_text(contract.to_yaml())
    os.replace(tmp_path, contract_path)


__all__: list[str] = [
    "ModelContractContext",
    "ModelContractGate",
    "ModelContractQuestion",
    "ModelContractRequirement",
    "ModelContractVerification",
    "ModelTicketContract",
    "extract_contract",
    "persist_contract_locally",
    "update_description_with_contract",
]
