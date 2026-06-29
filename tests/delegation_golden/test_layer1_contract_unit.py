# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Layer-1 delegation regression tests — contract/unit, PRE-MERGE CI gate (OMN-13540).

Deterministic. Run by omnimarket's existing pytest CI on EVERY PR, with NO live
model. These catch the import / wiring / config regression class — the class that
just took delegation down (OMN-13539 ImportError) — so they MUST hard-fail when
that class regresses.

  U1  node_delegate_skill_orchestrator contract loads + handler imports cleanly
      (regression for the ModelPremiumCounterfactual ImportError — OMN-13539).
  U2  every tier in the routing contract resolves to a complete backend
      (provider/endpoint/model/api_key_ref) — no None/missing.
  U3  ModelDelegateSkillRequest validates each allowed task_type + rejects unknown.
  U4  the terminal/projection schema carries model_name + token + cost fields
      (telemetry-drop regression — OMN-13535).

Assertions encode INTENDED behavior per the contract, resolved against the actual
omnimarket source (not assumed).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelBifrostDelegationConfig,
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    ModelDelegationConfig,
    parse_delegation_config_yaml,
)
from tests.delegation_golden.corpus_loader import ModelCorpus, load_corpus

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Source-of-truth paths (resolved from the actual omnimarket source tree).
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[2] / "src" / "omnimarket"
_ORCHESTRATOR_CONTRACT = (
    _SRC / "nodes" / "node_delegate_skill_orchestrator" / "contract.yaml"
)
_BIFROST_CONFIG = _SRC / "configs" / "bifrost_delegation.yaml"
_ROUTING_TIERS_CONFIG = _SRC / "configs" / "routing_tiers.yaml"


# ---------------------------------------------------------------------------
# Corpus shape — the corpus is the suite's data; prove it parses and is complete.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus() -> ModelCorpus:
    return load_corpus()


def test_corpus_loads_and_validates(corpus: ModelCorpus) -> None:
    """corpus.yaml parses into the typed model and carries the OMN-13540 cases."""
    assert corpus.ticket == "OMN-13540"
    unit_ids = {c.id for c in corpus.unit_cases()}
    integration_ids = {c.id for c in corpus.integration_cases()}
    assert unit_ids == {"U1", "U2", "U3", "U4"}, unit_ids
    assert integration_ids == {f"I{n}" for n in range(1, 10)}, integration_ids


def test_corpus_xfail_markers_cite_tracking_ticket(corpus: ModelCorpus) -> None:
    """Every known-broken (xfail) case names a real OMN tracking ticket + reason."""
    for case in corpus.integration_cases():
        if case.xfail is not None:
            assert case.xfail.ticket.startswith("OMN-")
            assert case.xfail.reason.strip()


# ---------------------------------------------------------------------------
# U1 — orchestrator contract loads + handler class imports cleanly.
# ---------------------------------------------------------------------------


class TestU1ContractAndHandlerImport:
    """Regression for OMN-13539: a transitive ImportError took the orchestrator
    down at bootstrap. Importing the handler module re-exercises that chain
    (handler -> ModelPremiumCounterfactual -> pricing) and MUST not raise.
    """

    def test_orchestrator_contract_parses(self) -> None:
        assert _ORCHESTRATOR_CONTRACT.is_file(), _ORCHESTRATOR_CONTRACT
        contract = yaml.safe_load(_ORCHESTRATOR_CONTRACT.read_text())
        assert contract["node_name"] == "node_delegate_skill_orchestrator"
        assert contract["node_type"] == "orchestrator"
        # The handler the runtime dispatches to must be declared.
        assert contract["handler"]["class"] == "HandlerDelegateSkill"

    def test_handler_module_imports_cleanly(self) -> None:
        """Importing the handler re-runs the full import chain that broke."""
        module = importlib.import_module(
            "omnimarket.nodes.node_delegate_skill_orchestrator.handlers."
            "handler_delegate_skill"
        )
        assert hasattr(module, "HandlerDelegateSkill")

    def test_premium_counterfactual_symbol_resolves(self) -> None:
        """The exact symbol whose ImportError (OMN-13539) broke delegation."""
        from omnibase_core.models.delegation.wire import (
            ModelPremiumCounterfactual,
        )

        assert ModelPremiumCounterfactual is not None

    def test_request_and_response_models_import(self) -> None:
        from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
            ModelDelegateSkillRequest,
        )
        from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
            ModelDelegateSkillResponse,
        )

        assert ModelDelegateSkillRequest is not None
        assert ModelDelegateSkillResponse is not None


# ---------------------------------------------------------------------------
# U2 — every routing tier resolves to a complete backend.
# ---------------------------------------------------------------------------


class TestU2EveryTierResolvesCompleteBackend:
    """Each tier model's backend_ref must map to a backend that resolves a
    complete (provider/endpoint/model/api_key_ref) tuple. A None endpoint with
    no endpoint_url_env, a null model_name, or a cloud backend with no secret_ref
    is an incomplete (broken) route and fails this gate.

    Intended behavior: routing is fully resolvable from the repo contract +
    overlay-named env vars — never a half-wired tier.
    """

    @pytest.fixture(scope="class")
    def tiers_and_backends(
        self,
    ) -> tuple[ModelDelegationConfig, dict[str, ModelDelegationBackendConfig]]:
        delegation_config = parse_delegation_config_yaml(
            _ROUTING_TIERS_CONFIG.read_text()
        )
        bifrost = ModelBifrostDelegationConfig.model_validate(
            yaml.safe_load(_BIFROST_CONFIG.read_text())
        )
        backends_by_id = {b.backend_id: b for b in bifrost.backends}
        return delegation_config, backends_by_id

    def test_every_tier_has_models(
        self,
        tiers_and_backends: tuple[
            ModelDelegationConfig, dict[str, ModelDelegationBackendConfig]
        ],
    ) -> None:
        delegation_config, _ = tiers_and_backends
        tiers = delegation_config.tiers
        assert tiers, "routing_tiers.yaml declared no tiers"
        for tier in tiers:
            assert tier.models, f"tier {tier.name!r} declared no models"

    def test_every_tier_model_maps_to_a_known_backend(
        self,
        tiers_and_backends: tuple[
            ModelDelegationConfig, dict[str, ModelDelegationBackendConfig]
        ],
    ) -> None:
        delegation_config, backends_by_id = tiers_and_backends
        for tier in delegation_config.tiers:
            for model in tier.models:
                assert model.backend_ref in backends_by_id, (
                    f"tier {tier.name!r} model {model.id!r} references unknown "
                    f"backend {model.backend_ref!r}; known: "
                    f"{sorted(backends_by_id)}"
                )

    def test_every_referenced_backend_resolves_complete_tuple(
        self,
        tiers_and_backends: tuple[
            ModelDelegationConfig, dict[str, ModelDelegationBackendConfig]
        ],
    ) -> None:
        delegation_config, backends_by_id = tiers_and_backends
        for tier in delegation_config.tiers:
            for model in tier.models:
                backend = backends_by_id[model.backend_ref]

                # provider/endpoint: a literal URL OR an env var name that the
                # overlay populates with the COMPLETE URL (OMN-12815). Exactly
                # one must be present — a backend with neither is unroutable.
                endpoint_resolvable = bool(backend.endpoint_url) or bool(
                    backend.endpoint_url_env
                )
                assert endpoint_resolvable, (
                    f"backend {backend.backend_id!r} (tier {tier.name!r}) has "
                    "neither endpoint_url nor endpoint_url_env"
                )

                # model: every backend must name the model it calls.
                assert backend.model_name, (
                    f"backend {backend.backend_id!r} (tier {tier.name!r}) has a "
                    "null/empty model_name"
                )

                # api_key_ref: cloud/metered backends MUST resolve a secret
                # reference; local (owned-GPU) backends legitimately have none.
                if backend.tier == "local":
                    continue
                assert backend.resolved_secret_ref, (
                    f"non-local backend {backend.backend_id!r} (tier "
                    f"{tier.name!r}) resolves no api_key_ref/secret_ref"
                )

    def test_default_and_ceiling_backends_resolvable(
        self,
        tiers_and_backends: tuple[
            ModelDelegationConfig, dict[str, ModelDelegationBackendConfig]
        ],
    ) -> None:
        """The ceiling-tier model (escalation target) must resolve a backend.

        Intended behavior (OMN-13351): the ceiling tier maps to a resolvable
        HTTP frontier backend, not a secret-dead one.
        """
        delegation_config, backends_by_id = tiers_and_backends
        tiers = list(delegation_config.tiers)
        ceiling = tiers[-1]
        assert ceiling.models, f"ceiling tier {ceiling.name!r} has no models"
        for model in ceiling.models:
            backend = backends_by_id[model.backend_ref]
            assert backend.resolved_secret_ref, (
                f"ceiling backend {backend.backend_id!r} resolves no secret_ref "
                "— escalation would terminate no_routable_backend"
            )


# ---------------------------------------------------------------------------
# U3 — ModelDelegateSkillRequest validates each allowed task_type, rejects unknown.
# ---------------------------------------------------------------------------


class TestU3RequestModelTaskTypeValidation:
    """The request model's task_type Literal must match contract allowed_task_types
    and reject anything outside it. Intended behavior: routing taxonomy is a
    closed set enforced at the request boundary.
    """

    @pytest.fixture(scope="class")
    def allowed_task_types(self) -> list[str]:
        contract = yaml.safe_load(_ORCHESTRATOR_CONTRACT.read_text())
        return list(contract["allowed_task_types"])

    def test_request_accepts_every_allowed_task_type(
        self, allowed_task_types: list[str]
    ) -> None:
        from omnimarket.models.delegation.wire.model_delegate_skill_request import (
            ModelDelegateSkillRequest,
        )

        for task_type in allowed_task_types:
            request = ModelDelegateSkillRequest(
                prompt="x",
                task_type=task_type,
                source="claude-code",
            )
            assert request.task_type == task_type

    def test_request_rejects_unknown_task_type(self) -> None:
        from omnimarket.models.delegation.wire.model_delegate_skill_request import (
            ModelDelegateSkillRequest,
        )

        with pytest.raises(ValidationError):
            ModelDelegateSkillRequest(
                prompt="x",
                task_type="definitely_not_a_task_type",
                source="claude-code",
            )

    def test_request_model_literal_matches_contract_allowed_set(
        self, allowed_task_types: list[str]
    ) -> None:
        """The model's task_type Literal must cover every contract-allowed value.

        A drift here means a task_type accepted by the contract would be rejected
        by the model (or vice versa) — a wiring regression.
        """
        from typing import get_args, get_type_hints

        from omnimarket.models.delegation.wire.model_delegate_skill_request import (
            ModelDelegateSkillRequest,
        )

        hints = get_type_hints(ModelDelegateSkillRequest)
        literal_values = set(get_args(hints["task_type"]))
        missing = set(allowed_task_types) - literal_values
        assert not missing, (
            f"contract allows task_types the request model rejects: {sorted(missing)}"
        )


# ---------------------------------------------------------------------------
# U4 — terminal/projection schema carries model_name + token + cost fields.
# ---------------------------------------------------------------------------


class TestU4TerminalProjectionTelemetrySchema:
    """Regression for OMN-13535: the terminal/projection schema dropped telemetry
    so completed rows landed with empty model/tokens/cost. The projection event
    model and the consumer-facing response metrics MUST declare these fields.

    Intended behavior: every terminal row can carry model_name + tokens + cost.
    """

    def test_projection_event_model_has_telemetry_fields(self) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
            ModelProjectionTaskDelegatedEvent,
        )

        fields = set(ModelProjectionTaskDelegatedEvent.model_fields.keys())
        for required in (
            "model_name",
            "tokens_input",
            "tokens_output",
            "cost_usd",
        ):
            assert required in fields, (
                f"projection event model missing telemetry field {required!r}; "
                f"has {sorted(fields)}"
            )

    def test_response_metrics_carry_token_and_cost_fields(self) -> None:
        from omnimarket.models.delegation.wire.model_delegate_skill_response import (
            ModelDelegateSkillResponse,
            ModelDelegateSkillResponseMetrics,
        )

        metric_fields = set(ModelDelegateSkillResponseMetrics.model_fields.keys())
        for required in ("input_tokens", "output_tokens", "total_tokens", "cost_usd"):
            assert required in metric_fields, (
                f"response metrics missing {required!r}; has {sorted(metric_fields)}"
            )

        response_fields = set(ModelDelegateSkillResponse.model_fields.keys())
        assert "model_name" in response_fields
        assert "metrics" in response_fields
