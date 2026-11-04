# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: replay the recorded DELETE pairs of this route against the mounted app.

Pending on T18b, OMN-22158: the test runs once the masked corpus lands and
ONEX_T18B_CORPUS_DIR names it. It fails on any difference outside the corpus's
named masking rules, and it never skips when pairs for this route are present.
"""

from __future__ import annotations

import pytest

from tests.nodes.t18b_replay_core import run_replay

pytestmark = pytest.mark.unit


def test_recorded_pairs_replay_with_no_unmasked_difference() -> None:
    run_replay("DELETE", "/v1/tenants/me/inference-credentials/{api_key_ref}")
