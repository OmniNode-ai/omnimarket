"""Pure deterministic generation of pytest artifacts from ticket contracts."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Any

from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

from omnimarket.nodes.node_test_generator_compute.models.model_test_generation_request import (
    ModelTestGenerationRequest,
)
from omnimarket.nodes.node_test_generator_compute.models.model_test_generation_result import (
    EnumTestGenerationStatus,
    ModelGeneratedTestFile,
    ModelTestGenerationResult,
)

TEMPLATE_VERSION = "ticket-contract-pytest-template-v1"


def _hash_json(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _contract_hash(contract: ModelTicketContract) -> str:
    return _hash_json(
        contract.model_dump(mode="json", exclude={"contract_fingerprint"})
    )


def _safe_name(value: str, *, fallback: str) -> str:
    name = re.sub(r"[^0-9a-zA-Z_]+", "_", value.strip().lower()).strip("_")
    if not name:
        name = fallback
    if name[0].isdigit():
        name = f"_{name}"
    return name


def _contract_has_machine_material(contract: ModelTicketContract) -> bool:
    return bool(
        contract.golden_path
        or contract.requirements
        or contract.dod_evidence
        or contract.evidence_requirements
        or contract.verification_steps
    )


def _source_refs(contract: ModelTicketContract) -> tuple[str, ...]:
    refs: list[str] = []
    for requirement in contract.requirements:
        refs.append(f"requirement:{requirement.id}")
        refs.extend(
            f"acceptance:{requirement.id}/{criterion.id}"
            for criterion in requirement.acceptance
        )
        refs.extend(
            f"proof:{requirement.id}/{proof.criterion_id}"
            for proof in requirement.proof_requirements
        )
    refs.extend(f"dod:{item.id}" for item in contract.dod_evidence)
    refs.extend(
        f"evidence:{requirement.kind.value}"
        for requirement in contract.evidence_requirements
    )
    if contract.golden_path is not None:
        refs.append("golden_path:input")
        refs.append("golden_path:output")
    refs.extend(f"verification:{step.id}" for step in contract.verification_steps)
    return tuple(refs)


def _manifest(contract: ModelTicketContract, contract_hash: str) -> dict[str, Any]:
    return {
        "ticket_id": contract.ticket_id,
        "title": contract.title,
        "schema_version": contract.schema_version,
        "contract_hash": contract_hash,
        "contract_fingerprint": contract.contract_fingerprint,
        "contract_completeness": contract.contract_completeness.value,
        "is_seam_ticket": contract.is_seam_ticket,
        "interface_change": contract.interface_change,
        "interfaces_touched": [
            surface.value for surface in contract.interfaces_touched
        ],
        "interfaces_provided": [
            item.model_dump(mode="json") for item in contract.interfaces_provided
        ],
        "interfaces_consumed": [
            item.model_dump(mode="json") for item in contract.interfaces_consumed
        ],
        "requirements": [
            {
                "id": requirement.id,
                "statement": requirement.statement,
                "acceptance": [
                    criterion.model_dump(mode="json")
                    for criterion in requirement.acceptance
                ],
                "proof_requirements": [
                    proof.model_dump(mode="json")
                    for proof in requirement.proof_requirements
                ],
            }
            for requirement in contract.requirements
        ],
        "golden_path": (
            contract.golden_path.model_dump(mode="json")
            if contract.golden_path is not None
            else None
        ),
        "dod_evidence": [
            item.model_dump(mode="json") for item in contract.dod_evidence
        ],
        "evidence_requirements": [
            item.model_dump(mode="json") for item in contract.evidence_requirements
        ],
        "verification_steps": [
            item.model_dump(mode="json") for item in contract.verification_steps
        ],
    }


def _append_requirement_tests(
    lines: list[str],
    manifest_var: str,
    contract: ModelTicketContract,
) -> tuple[str, ...]:
    node_ids: list[str] = []
    class_name = (
        f"Test{_safe_name(contract.ticket_id, fallback='ticket').upper()}Contract"
    )
    lines.extend(["", "@pytest.mark.unit", f"class {class_name}:"])
    lines.extend(
        [
            "    def test_contract_identity_present(self) -> None:",
            f"        assert {manifest_var}['ticket_id']",
            f"        assert {manifest_var}['title']",
        ]
    )
    node_ids.append(f"{class_name}::test_contract_identity_present")

    if contract.golden_path is not None:
        lines.extend(
            [
                "",
                "    def test_golden_path_topics_are_declared(self) -> None:",
                f"        golden_path = {manifest_var}['golden_path']",
                "        assert golden_path is not None",
                "        assert golden_path['input']['topic']",
                "        assert golden_path['output']['topic']",
            ]
        )
        node_ids.append(f"{class_name}::test_golden_path_topics_are_declared")

    for index, requirement in enumerate(contract.requirements):
        suffix = _safe_name(requirement.id, fallback=f"requirement_{index + 1}")
        lines.extend(
            [
                "",
                f"    def test_requirement_{suffix}_has_acceptance(self) -> None:",
                f"        requirement = {manifest_var}['requirements'][{index}]",
                "        assert requirement['id']",
                "        assert requirement['statement']",
                "        assert requirement['acceptance']",
            ]
        )
        node_ids.append(f"{class_name}::test_requirement_{suffix}_has_acceptance")

    if contract.dod_evidence:
        lines.extend(
            [
                "",
                "    def test_dod_evidence_declares_checks(self) -> None:",
                f"        for dod_item in {manifest_var}['dod_evidence']:",
                "            assert dod_item['id']",
                "            assert dod_item['description']",
                "            assert dod_item['checks']",
            ]
        )
        node_ids.append(f"{class_name}::test_dod_evidence_declares_checks")

    if contract.evidence_requirements:
        lines.extend(
            [
                "",
                "    def test_evidence_requirements_are_declared(self) -> None:",
                f"        for requirement in {manifest_var}['evidence_requirements']:",
                "            assert requirement['kind']",
                "            assert requirement['description']",
            ]
        )
        node_ids.append(f"{class_name}::test_evidence_requirements_are_declared")

    return tuple(node_ids)


def _render_test_file(
    contract: ModelTicketContract, contract_hash: str
) -> tuple[str, tuple[str, ...]]:
    manifest = _manifest(contract, contract_hash)
    manifest_text = json.dumps(manifest, indent=4, sort_keys=True, default=str)
    lines = [
        f'"""Generated acceptance scaffolding for {contract.ticket_id}.',
        "",
        "This file is produced by node_test_generator_compute and contains",
        "contract-derived assertions only. Runtime closeout remains authoritative.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import pytest",
        "",
        f"CONTRACT_MANIFEST = {manifest_text}",
    ]
    node_ids = _append_requirement_tests(lines, "CONTRACT_MANIFEST", contract)
    return "\n".join(lines) + "\n", node_ids


class HandlerTestGenerator:
    """Generate parser-valid pytest artifacts without I/O or side effects."""

    def handle(self, request: ModelTestGenerationRequest) -> ModelTestGenerationResult:
        contract = request.contract
        contract_hash = _contract_hash(contract)
        template_hash = _hash_text(TEMPLATE_VERSION)

        if not _contract_has_machine_material(contract):
            return ModelTestGenerationResult(
                status=EnumTestGenerationStatus.INSUFFICIENT_CONTRACT,
                ticket_id=contract.ticket_id,
                contract_hash=contract_hash,
                contract_fingerprint=contract.contract_fingerprint,
                generator_version=request.generator_version,
                template_hash=template_hash,
                generation_profile_hash=request.generation_profile_hash,
                generated_files=(),
                warnings=("INSUFFICIENT_CONTRACT: no machine-checkable material",),
            )

        content, node_ids = _render_test_file(contract, contract_hash)
        try:
            ast.parse(content)
        except SyntaxError as exc:
            return ModelTestGenerationResult(
                status=EnumTestGenerationStatus.FAILED,
                ticket_id=contract.ticket_id,
                contract_hash=contract_hash,
                contract_fingerprint=contract.contract_fingerprint,
                generator_version=request.generator_version,
                template_hash=template_hash,
                generation_profile_hash=request.generation_profile_hash,
                generated_files=(),
                warnings=(),
                failure_class="SYNTAX_INVALID",
                parser_error=str(exc),
            )

        file_path = (
            "generated_tests/"
            f"test_{_safe_name(contract.ticket_id, fallback='ticket')}_contract.py"
        )
        generated_file = ModelGeneratedTestFile(
            path=file_path,
            content=content,
            content_sha256=_hash_text(content),
            pytest_node_ids=node_ids,
            source_refs=_source_refs(contract),
        )
        return ModelTestGenerationResult(
            status=EnumTestGenerationStatus.OK,
            ticket_id=contract.ticket_id,
            contract_hash=contract_hash,
            contract_fingerprint=contract.contract_fingerprint,
            generator_version=request.generator_version,
            template_hash=template_hash,
            generation_profile_hash=request.generation_profile_hash,
            generated_files=(generated_file,),
            warnings=(),
        )


__all__ = ["HandlerTestGenerator"]
