"""Pure deterministic generation of expected golden-chain entries."""

from __future__ import annotations

import hashlib
import json

from omnibase_core.models.pipeline.model_golden_chain_entry import ModelGoldenChainEntry
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

from omnimarket.nodes.node_golden_chain_generator_compute.models.model_golden_chain_generation_request import (
    ModelGoldenChainGenerationRequest,
)
from omnimarket.nodes.node_golden_chain_generator_compute.models.model_golden_chain_generation_result import (
    EnumGoldenChainGenerationStatus,
    ModelDeferredChainWarning,
    ModelGoldenChainGenerationResult,
)


def _hash_json(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _contract_hash(contract: ModelTicketContract) -> str:
    return _hash_json(
        contract.model_dump(mode="json", exclude={"contract_fingerprint"})
    )


def _chain_hash(
    entries: tuple[ModelGoldenChainEntry, ...],
    warnings: tuple[ModelDeferredChainWarning, ...],
) -> str:
    return _hash_json(
        {
            "expected_chain": [entry.model_dump(mode="json") for entry in entries],
            "deferred_warnings": [
                warning.model_dump(mode="json") for warning in warnings
            ],
        }
    )


def _entries_from_contract(
    contract: ModelTicketContract,
) -> tuple[ModelGoldenChainEntry, ...]:
    if contract.golden_path is None:
        return ()

    return (
        ModelGoldenChainEntry(
            sequence=1,
            event_type="golden_path_input",
            topic=contract.golden_path.input.topic,
            source_node="UNKNOWN",
        ),
        ModelGoldenChainEntry(
            sequence=2,
            event_type="golden_path_output",
            topic=contract.golden_path.output.topic,
            source_node="UNKNOWN",
        ),
    )


def _warnings_from_contract(
    contract: ModelTicketContract,
) -> tuple[ModelDeferredChainWarning, ...]:
    warnings: list[ModelDeferredChainWarning] = []
    if contract.golden_path is None:
        warnings.append(
            ModelDeferredChainWarning(
                code="GOLDEN_PATH_MISSING",
                source_ref="contract.golden_path",
                detail="No explicit golden_path is declared on the contract.",
            )
        )
        return tuple(warnings)

    warnings.append(
        ModelDeferredChainWarning(
            code="SOURCE_NODE_UNKNOWN",
            source_ref="contract.golden_path.input.topic",
            detail=(
                "Input topic is explicit, but the publishing source node is not "
                "declared in the contract."
            ),
        )
    )
    warnings.append(
        ModelDeferredChainWarning(
            code="SOURCE_NODE_UNKNOWN",
            source_ref="contract.golden_path.output.topic",
            detail=(
                "Output topic is explicit, but the producing source node is not "
                "declared in the contract."
            ),
        )
    )
    if contract.golden_path.output.assertions:
        warnings.append(
            ModelDeferredChainWarning(
                code="ASSERTIONS_NOT_TOPOLOGY",
                source_ref="contract.golden_path.output.assertions",
                detail=(
                    "Output assertions are preserved for tests but do not imply "
                    "additional hidden event-chain entries."
                ),
            )
        )
    return tuple(warnings)


class HandlerGoldenChainGenerator:
    """Derive expected chain entries from explicit contract declarations only."""

    def handle(
        self,
        request: ModelGoldenChainGenerationRequest,
    ) -> ModelGoldenChainGenerationResult:
        contract = request.contract
        contract_hash = _contract_hash(contract)
        expected_chain = _entries_from_contract(contract)
        warnings = _warnings_from_contract(contract)

        if not expected_chain:
            status = EnumGoldenChainGenerationStatus.INSUFFICIENT_CONTRACT
        elif warnings:
            status = EnumGoldenChainGenerationStatus.DEFERRED
        else:
            status = EnumGoldenChainGenerationStatus.OK

        return ModelGoldenChainGenerationResult(
            status=status,
            ticket_id=contract.ticket_id,
            contract_hash=contract_hash,
            contract_fingerprint=contract.contract_fingerprint,
            chain_hash=_chain_hash(expected_chain, warnings),
            generator_version=request.generator_version,
            template_hash=request.template_hash,
            generated_test_hash=request.generated_test_hash,
            expected_chain=expected_chain,
            deferred_warnings=warnings,
        )


__all__ = ["HandlerGoldenChainGenerator"]
