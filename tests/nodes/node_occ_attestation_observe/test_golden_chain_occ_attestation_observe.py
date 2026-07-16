# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_occ_attestation_observe (OMN-14393).

Read-only EFFECT node. Satisfies golden-chain-coverage-gate and the "Golden Chain
Suite" CI job (collects ``tests/nodes/*/test_golden_chain_*.py``).

Covers contract/metadata structural validation plus an OFFLINE replay through the
real ``HandlerOccAttestationObserve.handle()`` path (I/O boundaries stubbed): a
byte-matching machine-minted companion produces a clean observation, and a
resolution error produces a fail-soft not-clean observation (report-only gate must
never raise). The full behavioural suite lives in
``tests/unit/nodes/node_occ_attestation_observe/test_attestation_observe.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_companion import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from omnimarket.nodes.node_occ_attestation_observe.handlers.handler_occ_attestation_observe import (
    HandlerOccAttestationObserve,
    strip_evidence_source_stamp,
)
from omnimarket.nodes.node_occ_attestation_observe.models.model_occ_attestation_observe_request import (
    ModelOccAttestationObserveRequest,
)
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    HandlerOccStateEffect,
)
from omnimarket.nodes.node_occ_state_effect.models.model_occ_state_request import (
    ModelOccStateRequest,
)

_OCC_HEAD_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f001234567"


@pytest.fixture
def node_dir() -> Path:
    return (
        Path(__file__).resolve().parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_occ_attestation_observe"
    )


def _request_with_stamp() -> ModelOccCompanionRequest:
    return ModelOccCompanionRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=1760,
        pr_head_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        pr_title="feat(OMN-14608): thing",
        pr_body="Closes OMN-14608\n\nEvidence-Source: OCC#4284",
        run_timestamp="2026-07-14T12:00:00Z",
        product_probe=ModelObservedProbe(
            command="gh pr view 1760",
            stdout='{"number":1760,"state":"OPEN"}',
            exit_code=0,
        ),
    )


def _expected_content(req: ModelOccCompanionRequest) -> dict[str, str]:
    v2 = req.model_copy(
        update={
            "pr_body": strip_evidence_source_stamp(req.pr_body),
            "occ_pr_number": 4284,
            "occ_head_sha": _OCC_HEAD_SHA,
            "occ_probe": ModelObservedProbe(
                command="gh pr view 4284 --repo OmniNode-ai/onex_change_control --json number,state",
                stdout='{"number":4284,"state":"OPEN"}',
                exit_code=0,
            ),
        }
    )
    return {f.path: f.content for f in compute_companion_plan(v2).companion_files}


class _StubState(HandlerOccStateEffect):
    def __init__(self, req: ModelOccCompanionRequest) -> None:
        self._req = req

    async def handle(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        return self._req


class _OfflineObserve(HandlerOccAttestationObserve):
    def __init__(self, req: ModelOccCompanionRequest, content: dict[str, str]) -> None:
        super().__init__(state_handler=_StubState(req))
        self._content = content

    def _resolve_github_token(self) -> str:
        return "token"

    def _read_occ_preflight_eligible(
        self, repo: str, head_sha: str, token: str
    ) -> bool:
        return True

    def _read_occ_pr_head_and_marker(
        self, occ_repo: str, occ_pr_number: int, token: str
    ) -> tuple[str, bool]:
        return _OCC_HEAD_SHA, True

    def _content_at_ref(self, repo: str, path: str, ref: str, token: str) -> str | None:
        return self._content.get(path)


class TestContractYaml:
    def test_contract_loads_as_effect_node(self, node_dir: Path) -> None:
        data = yaml.safe_load((node_dir / "contract.yaml").read_text())
        assert data["name"] == "node_occ_attestation_observe"
        assert data["node_type"] == "EFFECT_GENERIC"

    def test_contract_declares_observe_operation(self, node_dir: Path) -> None:
        data = yaml.safe_load((node_dir / "contract.yaml").read_text())
        ops = {
            h["operation"]: h["handler"]["name"]
            for h in data["handler_routing"]["handlers"]
        }
        assert ops.get("observe_attestation") == "HandlerOccAttestationObserve"

    def test_contract_is_directly_invoked_and_declares_read_only_secret(
        self, node_dir: Path
    ) -> None:
        # No event_bus / top-level terminal_event => _run_compute path preserved;
        # runtime_dispatch declares the dispatch-addressable command seam only.
        data = yaml.safe_load((node_dir / "contract.yaml").read_text())
        assert "event_bus" not in data
        assert "terminal_event" not in data
        assert (
            data["runtime_dispatch"]["command_topic"]
            == "onex.cmd.omnimarket.occ-attestation-observe-requested.v1"
        )
        assert data["secrets"]["GITHUB_TOKEN"]["required"] is True

    def test_output_model_is_the_shared_observation(self, node_dir: Path) -> None:
        data = yaml.safe_load((node_dir / "contract.yaml").read_text())
        assert data["output_model"]["name"] == "ModelOccAutoauthorObservation"


class TestGoldenChainReplay:
    async def test_byte_matching_companion_yields_clean_observation(self) -> None:
        req = _request_with_stamp()
        handler = _OfflineObserve(req, _expected_content(req))
        obs = await handler.handle(
            ModelOccAttestationObserveRequest(
                repo="OmniNode-ai/omnimarket", pr_number=1760
            )
        )
        assert isinstance(obs, ModelOccAutoauthorObservation)
        assert obs.occ_pr_number == 4284
        assert obs.is_clean is True

    async def test_replay_is_deterministic(self) -> None:
        req = _request_with_stamp()
        content = _expected_content(req)
        request = ModelOccAttestationObserveRequest(
            repo="OmniNode-ai/omnimarket", pr_number=1760
        )
        a = await _OfflineObserve(req, content).handle(request)
        b = await _OfflineObserve(req, content).handle(request)
        # observed_at differs by construction; the verdict fields are stable.
        assert (a.minted_by_node, a.attestation_match, a.occ_preflight_eligible) == (
            b.minted_by_node,
            b.attestation_match,
            b.occ_preflight_eligible,
        )

    async def test_handler_is_fail_soft(self) -> None:
        class _Boom(HandlerOccAttestationObserve):
            def _resolve_github_token(self) -> str:
                raise RuntimeError("secret store down")

        obs = await _Boom().handle(
            ModelOccAttestationObserveRequest(
                repo="OmniNode-ai/omnimarket", pr_number=1760
            )
        )
        assert isinstance(obs, ModelOccAutoauthorObservation)
        assert obs.is_clean is False
