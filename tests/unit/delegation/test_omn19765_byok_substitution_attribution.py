# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19765 AC1: a BYOK-substituted attempt names which backend it replaced.

``substitute_local_byok_route`` replaces a HOUSE rung (e.g. the platform
``cloud-glm`` backend) with the customer's own declared BYOK rung
(``byok-glm``) before dispatch. Before this change the resolved backend
carried no memory of what it replaced, so a caller who pinned ``cloud-glm``
had no way to tell "this ran on the customer's own key for the SAME backend
I pinned" apart from "this escalated to something else entirely" -- both
looked identical: an accepted attempt naming a backend_id other than the pin.

Found by lane glm-key-reachable-83 (OMN-19124): run
``86538bdd-0a35-47c6-98e1-b4d13ad39e55`` answered on the first attempt by
glm-5.3-flash via the BYOK route, yet the CLI's pin check
(``omnibase_infra`` ``_backend_pin_defect``) reported a false escalation
because it had only the raw ``backend_id`` strings to compare.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.inference.local_byok_credential_adapter import (
    register_local_byok_credential,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.routing.local_byok_route import substitute_local_byok_route

pytestmark = pytest.mark.unit

_FAKE_CUSTOMER_KEY = "sk-glm-" + "c" * 40


@pytest.fixture
def local_db(tmp_path: Path) -> Path:
    return tmp_path / "delegation.sqlite"


def _cloud_glm_house_rung() -> ModelResolvedDelegationBackend:
    """The platform's own ``cloud-glm`` rung, shaped like bifrost yields it."""
    return ModelResolvedDelegationBackend(
        backend_id="cloud-glm",
        model_id="glm-5.3-flash",
        endpoint_ref="https://api.z.ai/api/coding/paas/v4/chat/completions",
        tier="cheap_cloud",
        max_tokens=4096,
        timeout_ms=30000,
        secret_ref="llm.glm.api_key",
    )


class TestAc1SubstitutionNamesTheBackendItReplaced:
    """Falsifier: a substituted attempt that does not name ``cloud-glm``."""

    def test_a_pinned_cloud_glm_run_with_a_registered_key_names_cloud_glm(
        self, local_db: Path
    ) -> None:
        register_local_byok_credential("glm", _FAKE_CUSTOMER_KEY, db_path=local_db)

        routed = substitute_local_byok_route(_cloud_glm_house_rung(), db_path=local_db)

        # The route that answers is the customer-paid BYOK backend...
        assert routed.backend_id == "byok-glm"
        # ...but it names the platform rung a caller's pin actually named.
        assert routed.substituted_from_backend_id == "cloud-glm"

    def test_an_unsubstituted_rung_names_no_replacement(self, local_db: Path) -> None:
        """No registered key -> the house rung is returned untouched (existing
        behaviour) and, being untouched, carries no substitution attribution."""
        routed = substitute_local_byok_route(_cloud_glm_house_rung(), db_path=local_db)

        assert routed.backend_id == "cloud-glm"
        assert routed.substituted_from_backend_id is None

    def test_a_keyless_local_rung_is_unchanged_and_unattributed(
        self, local_db: Path
    ) -> None:
        register_local_byok_credential("glm", _FAKE_CUSTOMER_KEY, db_path=local_db)
        local_rung = ModelResolvedDelegationBackend(
            backend_id="local-coder",
            model_id="Qwen3.8-27B",
            endpoint_ref="http://localhost:8080/v1/chat/completions",
            tier="local",
            max_tokens=4096,
            timeout_ms=30000,
            secret_ref=None,
        )

        routed = substitute_local_byok_route(local_rung, db_path=local_db)

        assert routed is local_rung
        assert routed.substituted_from_backend_id is None
