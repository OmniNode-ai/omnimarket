# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B handler: explicit text in, the canonical OMN-2362 validation report out.

Pure: no filesystem, clock, environment, subprocess or network access. Every read
happens in ``runtime_projection_contract_check``, the node's only EFFECT boundary.
"""

from typing import Final, Literal

from omnibase_core.models.validation.model_validation_finding import (
    ModelValidationFinding,
)
from omnibase_core.models.validation.model_validation_report import (
    ModelValidationFindingEmbed,
    ModelValidationReport,
    ModelValidationRequestRef,
)

from omnimarket.models.contract_projection_check import (
    EnumProjectionContractRule,
    ModelProjectionContractCheckInput,
)
from omnimarket.nodes.node_contract_projection_check_compute.handlers.check_access import (
    check_access,
)
from omnimarket.nodes.node_contract_projection_check_compute.handlers.check_cursor import (
    check_cursor,
)
from omnimarket.nodes.node_contract_projection_check_compute.handlers.check_dlq import (
    check_dlq,
)
from omnimarket.nodes.node_contract_projection_check_compute.handlers.findings import (
    RULE_ZERO_SCAN,
    VALIDATOR_ID,
    make_finding,
)

_PROFILE: Final[Literal["strict", "default", "advisory"]] = "default"


class HandlerProjectionContractCheck:
    def handle(
        self, request: ModelProjectionContractCheckInput
    ) -> ModelValidationReport:
        findings: list[ModelValidationFinding] = []
        scanned = (
            len(request.handlers)
            if request.rule is EnumProjectionContractRule.DLQ
            else len(request.nodes)
        )
        if request.require_scanned and scanned == 0:
            findings.append(
                make_finding(
                    rule_id=RULE_ZERO_SCAN,
                    message=f"rule {request.rule.value} gathered zero inputs; a scan of nothing is not a PASS",
                    location=None,
                    severity="ERROR",
                )
            )
        elif request.rule is EnumProjectionContractRule.ACCESS:
            findings.extend(check_access(request.nodes))
        elif request.rule is EnumProjectionContractRule.DLQ:
            findings.extend(check_dlq(request.handlers))
        else:
            findings.extend(check_cursor(request.nodes, request.cursor_baseline))
        return ModelValidationReport.from_findings(
            findings=tuple(
                ModelValidationFindingEmbed(**f.model_dump(mode="json"))
                for f in findings
            ),
            request=ModelValidationRequestRef(profile=_PROFILE),
            validators_run=(VALIDATOR_ID,),
        )
