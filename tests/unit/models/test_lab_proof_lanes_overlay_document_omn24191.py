# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the overlay home validates overlays/lab_proof_lanes/overlay.yaml through (OMN-24191)."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_lab_proof_lanes_overlay import (
    ModelLabProofLanesOverlay,
)

pytestmark = pytest.mark.unit

GOOD_LANE = {
    "name": "lane-a",
    "lane_role": "stability",
    "compose_project": "omnibase-infra-lane-a",
    "bus_lane": "lane-a",
    "docker_runs_on": ["self-hosted", "label-a"],
    "ingress_url": "http://192.0.2.10:18085",
    "projection_url": "http://192.0.2.10:13002",
    "deploy_agent_url": "http://192.0.2.10:8098",
}

GOOD_DOCUMENT = {
    "schema_version": 1,
    "variable": "LAB_PROOF_LANES_JSON",
    "repository": "OmniNode-ai/omnibase_infra",
    "lanes": [GOOD_LANE],
}

GOLDEN_VALUE = (
    '[{"name":"lane-a","lane_role":"stability",'
    '"compose_project":"omnibase-infra-lane-a",'
    '"bus_lane":"lane-a",'
    '"docker_runs_on":["self-hosted","label-a"],'
    '"ingress_url":"http://192.0.2.10:18085",'
    '"projection_url":"http://192.0.2.10:13002",'
    '"deploy_agent_url":"http://192.0.2.10:8098"}]'
)

_BAD_URLS = [
    "http://localhost:8085",
    "http://host.docker.internal:8085",
    "http://127.0.0.1:8085",
    "http://[::1]:8085",
    "http://192.0.2.10",
    "http://192.0.2.10:8085/health",
    "ftp://192.0.2.10:8085",
]

_URL_FIELDS = ["ingress_url", "projection_url", "deploy_agent_url", "gateway_url"]


def _with_lane(**changes: object) -> dict[str, object]:
    doc = copy.deepcopy(GOOD_DOCUMENT)
    doc["lanes"][0].update(changes)
    return doc


def _build_wrong_documents() -> list[dict[str, object]]:
    docs: list[dict[str, object]] = []
    docs.append({**copy.deepcopy(GOOD_DOCUMENT), "stray": 1})
    docs.append({**copy.deepcopy(GOOD_DOCUMENT), "schema_version": 2})
    docs.append({**copy.deepcopy(GOOD_DOCUMENT), "variable": "LAB_LANES_JSON"})
    docs.append(
        {**copy.deepcopy(GOOD_DOCUMENT), "repository": "OmniNode-ai/other_repository"}
    )
    docs.append({**copy.deepcopy(GOOD_DOCUMENT), "lanes": []})
    doc = copy.deepcopy(GOOD_DOCUMENT)
    doc["lanes"].append(copy.deepcopy(GOOD_LANE))
    docs.append(doc)
    docs.append(_with_lane(lane_role="dev"))
    docs.append(_with_lane(stray="x"))
    for field in _URL_FIELDS:
        for url in _BAD_URLS:
            docs.append(_with_lane(**{field: url}))
    docs.append(_with_lane(compose_project="other-project"))
    docs.append(_with_lane(compose_project="omnibase-infra_x"))
    docs.append(_with_lane(name="Bad Name"))
    docs.append(_with_lane(bus_lane="Bad Lane"))
    docs.append(_with_lane(docker_runs_on=[]))
    docs.append(_with_lane(docker_runs_on=[""]))
    return docs


_WRONG_DOCUMENTS = _build_wrong_documents()


def test_golden_document_renders_exact_value() -> None:
    overlay = ModelLabProofLanesOverlay.model_validate(GOOD_DOCUMENT)
    rendered = overlay.render_variable_value()
    assert rendered == GOLDEN_VALUE
    parsed = json.loads(rendered)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert list(parsed[0].keys()) == list(GOOD_LANE.keys())


def test_golden_document_with_gateway_renders_stripped_url() -> None:
    doc = _with_lane(gateway_url="http://192.0.2.10:18090/")
    overlay = ModelLabProofLanesOverlay.model_validate(doc)
    rendered = overlay.render_variable_value()
    parsed = json.loads(rendered)
    lane = parsed[0]
    assert lane["gateway_url"] == "http://192.0.2.10:18090"
    assert list(lane.keys())[-1] == "gateway_url"


@pytest.mark.parametrize("document", _WRONG_DOCUMENTS)
def test_a_wrong_document_refuses(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelLabProofLanesOverlay.model_validate(document)


def test_good_document_validates_and_host_local_alias_refused() -> None:
    overlay = ModelLabProofLanesOverlay.model_validate(GOOD_DOCUMENT)
    assert overlay.schema_version == 1
    assert overlay.variable == "LAB_PROOF_LANES_JSON"
    doc = _with_lane(ingress_url="http://localhost:8085")
    with pytest.raises(ValidationError, match="host-local alias"):
        ModelLabProofLanesOverlay.model_validate(doc)
