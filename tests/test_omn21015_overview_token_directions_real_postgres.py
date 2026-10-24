# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real SQL token totals proof, using only the caller's disposable CI database.

Failure modes: a direction summed over a different population no longer adds
up to tokens_total; the event branch maps tokens_input and tokens_output the
wrong way round; an estimate's tokens replace a matched run's event tokens;
one tenant's tokens reach another tenant's row; a run with zero tokens is
dropped. A skipped database is not a RED/GREEN receipt. The suite starts no
database. Run in hosted CI, never against the dev database.
"""

from __future__ import annotations

import asyncpg
import pytest

from tests.test_omn21206_view_population_real_postgres import (
    OTHER,
    OVERVIEW,
    TENANT,
    estimate,
    event,
    row,
)
from tests.test_omn21206_view_population_real_postgres import (
    views as _views,
)

pytestmark = pytest.mark.integration
views = _views


async def test_directions_sum_to_tokens_total_over_every_run(
    views: asyncpg.Connection,
) -> None:
    await event(views, "matched", "metered", 14, 1)
    await estimate(views, "matched", 910, 1)
    await event(views, "estimated", "manifest_compute", 22, 4)
    await event(views, "unknown", "", 33, 7)
    await estimate(views, "savings-only", 44, 2, row_id=2)
    await event(views, "other-tenant", "metered", 500, 9, OTHER)

    await views.execute(
        """UPDATE delegation_events
           SET tokens_input=19, tokens_output=3
           WHERE tenant_id=$1 AND correlation_id='estimated'""",
        TENANT,
    )
    await views.execute(
        """UPDATE delegation_events
           SET tokens_input=30, tokens_output=3
           WHERE tenant_id=$1 AND correlation_id='unknown'""",
        TENANT,
    )
    await views.execute(
        """UPDATE savings_estimates
           SET prompt_tokens=40, completion_tokens=4
           WHERE tenant_id=$1 AND session_id='savings-only'""",
        str(TENANT),
    )

    overview = await row(views, OVERVIEW)
    assert overview["tokens_input_total"] == 103
    assert overview["tokens_output_total"] == 10
    assert overview["tokens_total"] == 113
    assert (
        overview["tokens_input_total"] + overview["tokens_output_total"]
        == overview["tokens_total"]
    )

    other = await row(views, OVERVIEW, OTHER)
    assert other["tokens_input_total"] == 500
    assert other["tokens_output_total"] == 0


async def test_matched_run_keeps_its_event_directions(
    views: asyncpg.Connection,
) -> None:
    await event(views, "matched", "metered", 14, 1)
    await estimate(views, "matched", 910, 1)

    await views.execute(
        """UPDATE savings_estimates
           SET completion_tokens=90
           WHERE session_id='matched'"""
    )

    overview = await row(views, OVERVIEW)
    assert overview["tokens_input_total"] == 14
    assert overview["tokens_output_total"] == 0


async def test_zero_token_run_still_counts(views: asyncpg.Connection) -> None:
    await estimate(views, "zero", 0, 1)

    overview = await row(views, OVERVIEW)
    assert overview["run_count"] == 1
    assert overview["tokens_input_total"] == 0
    assert overview["tokens_output_total"] == 0
    assert overview["tokens_input_total"] is not None
    assert overview["tokens_output_total"] is not None
