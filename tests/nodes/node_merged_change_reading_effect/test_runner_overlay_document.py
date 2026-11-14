# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runner overlay document: the shape the overlays repository's merged_change_runners file must have."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_merged_change_reading_effect.models.model_merged_change_reading_run import (
    ModelMcrRunnerOverlay,
)

DOCUMENT = """
manifest_url: http://127.0.0.1:8085/v1/introspection/manifest
store_checkouts:
  - {repo: overlays, path: /srv/store/overlays}
unit_hosts:
  - {host: h051, ssh_host_env: ONEX_MCR_SSH_H051}
runners:
  - {repo: omnimarket, number: 1, kind: host_unit, unit: u.service, host: h051}
  - {repo: omnimarket, number: 2, kind: schedule, nodes: [node_a], trigger_topics: [t.tick], shared_result_topics: [t.shared]}
  - {repo: omnimarket, number: 3, kind: route, nodes: [node_b], no_caller: true}
"""


def test_the_document_loads_with_every_section() -> None:
    overlay = ModelMcrRunnerOverlay.model_validate(yaml.safe_load(DOCUMENT))
    assert overlay.manifest_url.endswith("/v1/introspection/manifest")
    assert overlay.store_checkouts[0].repo == "overlays"
    assert overlay.unit_hosts[0].ssh_host_env == "ONEX_MCR_SSH_H051"
    assert [r.number for r in overlay.runners] == [1, 2, 3]
    assert overlay.runners[2].no_caller is True


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(stray=1),
        lambda d: d["runners"][0].update(stray=1),
        lambda d: d.update(manifest_url="not a url"),
        lambda d: d["store_checkouts"][0].update(path="relative/path"),
        lambda d: d["unit_hosts"][0].update(ssh_host_env="bad name"),
        lambda d: d["runners"][0].update(number=0),
    ],
)
def test_failing_control_a_malformed_document_is_refused(
    mutation: Callable[[dict[str, Any]], object],
) -> None:
    document = yaml.safe_load(DOCUMENT)
    mutation(document)
    with pytest.raises(ValidationError):
        ModelMcrRunnerOverlay.model_validate(document)
