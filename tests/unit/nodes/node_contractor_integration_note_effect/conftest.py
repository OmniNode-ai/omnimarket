# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures for the contractor integration-note tests (OMN-17277)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_request import (
    ModelContractorRoster,
    ModelContractorRosterEntry,
    ModelMergedPullRequest,
    ModelPinRecipe,
    ModelTicketFacts,
)

CONTRACTOR_ID = "df034ef3-16f7-40d8-a138-1bac1d254cbf"
STAFF_ID = "7a850ce1-f95e-431f-b4e3-62f7449f04c0"


@pytest.fixture
def roster() -> ModelContractorRoster:
    return ModelContractorRoster(
        contractors=(
            ModelContractorRosterEntry(
                linear_user_id=CONTRACTOR_ID,
                display_name="Lakshman Patel",
                surfaces=("C1", "C4"),
            ),
        ),
        default_pin_recipe=ModelPinRecipe(
            template='uv pip install "git+{repo_url}@{merge_sha}"'
        ),
    )


@pytest.fixture
def pull_request() -> ModelMergedPullRequest:
    return ModelMergedPullRequest(
        repo="OmniNode-ai/omnibase_infra",
        number=3120,
        title="fix(OMN-17150): resolve the Bifrost lane overlay from the lane's own pin",
        body=(
            "## OMN-17150 — every lane fell through to the dev lane's overlay\n\n"
            "The renderer carried a hardcoded default overlay path naming the "
            "dev lane's file, so any lane that did not pass one explicitly "
            "resolved the dev lane's overlay.\n\n"
            "## The fix\n\nNo default overlay path, ever.\n"
        ),
        merge_sha="3a29fd26d0000000000000000000000000000000",
        merged_at=datetime(2026, 9, 1, 15, 21, 4, tzinfo=UTC),
        base_ref="dev",
        html_url="https://github.com/OmniNode-ai/omnibase_infra/pull/3120",
    )


@pytest.fixture
def contractor_ticket() -> ModelTicketFacts:
    return ModelTicketFacts(
        issue_id="11111111-2222-3333-4444-555555555555",
        identifier="OMN-17150",
        title="Bifrost lane overlay resolution",
        assignee_linear_user_id=CONTRACTOR_ID,
    )
