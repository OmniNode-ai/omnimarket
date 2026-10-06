# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A local rung bound to an endpoint but to no model is probeable before dispatch.

OMN-17427 / process top-20 item 8. The delegation route refuses every local rung
with ``local_model_binding_missing`` when a host overlay supplies the
``local-coder`` endpoint but not its served model id: the packaged contract
leaves ``model_name`` null on purpose (OMN-17099), so the overlay is the only
place the id can come from. The refusal fires at dispatch, one lane at a time.

The fixture below is that host's overlay in shape: an endpoint for each local
chat rung and no ``model_name``. The probe is ``cli_explain_routing
--fail-on-unbound-local``; it reads the merge delegation resolves and exits 1
naming each unbound backend and the overlay file that bound its endpoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.cli.cli_explain_routing import main
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.routing.delegation_backend_resolution import (
    load_bifrost_backends_with_provenance,
)

pytestmark = pytest.mark.unit

_PACKAGED_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)
_ENDPOINT = "http://local-rung.invalid:8000/v1/chat/completions"
_CHAT_RUNGS = ("local-coder", "local-heavy-reasoning")


def _overlay(tmp_path: Path, *, model_name: str | None) -> Path:
    rows: list[dict[str, Any]] = []
    for backend_id in _CHAT_RUNGS:
        row: dict[str, Any] = {"backend_id": backend_id, "endpoint_url": _ENDPOINT}
        if model_name is not None:
            row["model_name"] = model_name
        rows.append(row)
    path = tmp_path / "bifrost_overrides.yaml"
    path.write_text(yaml.safe_dump({"backends": rows}), encoding="utf-8")
    return path


def _probe(overlay: Path, *extra: str) -> int:
    return main(
        [
            "--contract-path",
            str(_PACKAGED_CONTRACT),
            "--overlay-path",
            str(overlay),
            *extra,
        ]
    )


def test_probe_refuses_a_local_endpoint_bound_to_no_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = _overlay(tmp_path, model_name=None)

    assert _probe(overlay, "--fail-on-unbound-local") == 1

    out = capsys.readouterr().out
    for backend_id in _CHAT_RUNGS:
        assert f"backends[{backend_id}].model_name" in out
    assert str(overlay) in out
    assert "local_model_binding_missing" in out


def test_report_names_the_unbound_rung_without_failing_unless_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = _overlay(tmp_path, model_name=None)

    assert _probe(overlay) == 0
    assert "local_model_binding_missing" in capsys.readouterr().out


def test_probe_is_clean_when_the_overlay_names_the_served_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = _overlay(tmp_path, model_name="lane-served-model")

    assert _probe(overlay, "--fail-on-unbound-local") == 0
    assert "local_model_binding_missing" not in capsys.readouterr().out


def test_a_local_rung_with_no_endpoint_is_not_unbound(tmp_path: Path) -> None:
    """The packaged base alone has null endpoints and null models: not a binding."""
    absent = tmp_path / "absent.yaml"

    assert _probe(absent, "--fail-on-unbound-local") == 0


def test_probe_flags_exactly_what_the_routing_reducer_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One definition of 'unbound': the probe and the dispatch-time refusal agree."""
    overlay = _overlay(tmp_path, model_name=None)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(_PACKAGED_CONTRACT))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    routing._config = None
    routing._load_bifrost_endpoints.cache_clear()
    try:
        with pytest.raises(
            ProtocolConfigurationError, match="local_model_binding_missing"
        ):
            routing._load_bifrost_endpoints()
    finally:
        routing._config = None
        routing._load_bifrost_endpoints.cache_clear()

    provenance = load_bifrost_backends_with_provenance(
        config_path=_PACKAGED_CONTRACT, overlay_path=overlay
    )[1]
    assert {f.backend_id for f in provenance.unbound_local_backends()} == set(
        _CHAT_RUNGS
    )
