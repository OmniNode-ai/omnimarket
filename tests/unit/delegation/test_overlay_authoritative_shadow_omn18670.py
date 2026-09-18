# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A machine-local overlay override of a tracked model_name is attributable.

OMN-18670. Every test here drives a FIXTURE OVERLAY that really does override a
field the committed contract really does declare — the 2026-09-18 case, rebuilt
on tmp paths — and asserts that the override names itself on each of the three
surfaces an operator can reach: the load-time log, the fail-closed
``model_attribution_mismatch`` refusal, and the explain command.

The case being reproduced: ``~/.omninode/delegation/bifrost_overrides.yaml``
(untracked, mtime 2026-09-06) pinned ``model_name: Qwen3.6-35B-A3B`` on both
``.201:8000`` rungs. The committed contract, the published wheel, the ``.201``
dev lane and the dispatch venv had all been repointed to ``Qwen3.8-27B`` by
``omnimarket#2627``. The local rung refused, delegation climbed to a cloud
model, and the refusal named the value but no file — so three lanes re-derived
the resolution path by hand.

The falsifier each test is written against is the one the ticket names:
a refusal with no source path, a silent override, an explain surface that does
not name a source per key, or a test that passes without the fixture actually
overriding anything. The last is guarded explicitly: every fixture asserts the
committed value and the overlay value DIFFER before exercising the surface.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    build_overlay_field_provenance,
    load_bifrost_delegation_config,
    warn_overlay_shadowed_authoritative_fields,
)
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.models.delegation.model_bifrost_overlay_provenance import (
    EnumBifrostFieldSource,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    HandlerLlmDelegationCall,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_request import (
    ModelLlmDelegationCallRequest,
)
from omnimarket.routing.delegation_backend_resolution import (
    load_bifrost_backends_with_provenance,
    resolve_delegation_backend,
)

_HANDLER_MODULE = (
    "omnimarket.nodes.node_llm_delegation_call_effect.handlers."
    "handler_llm_delegation_call"
)

#: The retired literal the stale overlay still pinned on 2026-09-18.
_RETIRED_MODEL = "Qwen3.6-35B-A3B"
#: What every committed surface had already been repointed to.
_SERVED_MODEL = "Qwen3.8-27B"

#: The committed contract, reduced to the two rungs the case turns on, for the
#: pure-compute tests that take backend lists directly. Both declare
#: ``model_name`` (the contract owns it) and leave ``endpoint_url`` null (the
#: overlay is meant to supply it) — the same split the real
#: ``src/omnimarket/configs/bifrost_delegation.yaml`` draws, asserted against
#: the real file by ``_write_pair`` below.
_COMMITTED_BACKENDS: list[dict[str, Any]] = [
    {
        "backend_id": "local-coder",
        "provider": "vllm",
        "endpoint_url": None,
        "model_name": _SERVED_MODEL,
        "tier": "local",
        "timeout_ms": 600000,
        "max_tokens": 8192,
    },
    {
        "backend_id": "local-heavy-reasoning",
        "provider": "vllm",
        "endpoint_url": None,
        "model_name": _SERVED_MODEL,
        "tier": "local",
        "timeout_ms": 600000,
        "max_tokens": 8192,
    },
]

#: The stale machine-local overlay, verbatim in shape: it supplies the site
#: endpoint (legitimate — the contract left it null) AND re-pins model_name to
#: the retired literal (the override this ticket exists to make attributable).
_OVERRIDDEN_BACKEND_IDS = ("local-coder", "local-heavy-reasoning")
_LOCAL_ENDPOINT = "http://local-rung.invalid:8000/v1/chat/completions"
_STALE_OVERLAY: dict[str, Any] = {
    "backends": [
        {
            "backend_id": backend_id,
            "endpoint_url": _LOCAL_ENDPOINT,
            "model_name": _RETIRED_MODEL,
        }
        for backend_id in _OVERRIDDEN_BACKEND_IDS
    ]
}

#: The marker every shadow WARN line carries, used instead of substring-matching
#: a path so an unrelated log line mentioning the overlay cannot be miscounted
#: as a shadow warning.
_WARN_MARKER = "bifrost_overlay_shadows_authoritative_field"

#: The REAL committed contract. The fixture overlay is merged over this rather
#: than over a hand-written stub, so the test reproduces the actual 2026-09-18
#: merge and cannot drift from the schema the loader validates.
_REAL_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)


def _write_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Copy the real committed contract, write the stale overlay, prove they differ.

    The ticket's AC4 falsifier is "the test passes without the fixture actually
    overriding anything", so the disagreement is asserted here rather than
    assumed — every path-based test in this module routes through this helper.
    Asserting it against the REAL contract also means that nulling or retiring
    ``model_name`` on these rungs breaks this test loudly instead of quietly
    draining it of meaning.
    """
    contract = tmp_path / "bifrost_delegation.yaml"
    overlay = tmp_path / "bifrost_overrides.yaml"
    contract.write_text(_REAL_CONTRACT.read_text(encoding="utf-8"), encoding="utf-8")
    overlay.write_text(yaml.safe_dump(_STALE_OVERLAY), encoding="utf-8")

    committed = yaml.safe_load(contract.read_text(encoding="utf-8"))
    declared = {
        row["backend_id"]: row.get("model_name")
        for row in committed["backends"]
        if row.get("backend_id") in _OVERRIDDEN_BACKEND_IDS
    }
    assert set(declared) == set(_OVERRIDDEN_BACKEND_IDS), (
        "the committed contract no longer declares the rungs this case turns on"
    )
    for backend_id, committed_value in declared.items():
        assert committed_value, (
            f"{backend_id} declares no model_name in the committed contract, so "
            "the overlay would be filling a null rather than overriding — this "
            "module would prove nothing"
        )
        assert committed_value != _RETIRED_MODEL, (
            f"{backend_id} declares the retired literal in the committed "
            "contract; the fixture overlay overrides nothing"
        )
    return contract, overlay


class TestOverlayShadowIsRecorded:
    """AC2 (record half): the merge produces a per-field source record."""

    @pytest.mark.unit
    def test_overlay_write_over_a_declared_model_name_is_recorded_as_a_shadow(
        self,
    ) -> None:
        provenance = build_overlay_field_provenance(
            _COMMITTED_BACKENDS,
            _STALE_OVERLAY["backends"],
            contract_source="src/omnimarket/configs/bifrost_delegation.yaml",
            overlay_source="/fixture-site/delegation/bifrost_overrides.yaml",
        )

        shadows = provenance.shadows()
        assert {s.backend_id for s in shadows} == {
            "local-coder",
            "local-heavy-reasoning",
        }
        for shadow in shadows:
            assert shadow.field_name == "model_name"
            assert shadow.value == _RETIRED_MODEL
            assert shadow.shadowed_value == _SERVED_MODEL
            assert shadow.source is EnumBifrostFieldSource.OVERLAY
            assert "bifrost_overrides.yaml" in shadow.source_ref
            described = shadow.describe()
            assert "bifrost_overrides.yaml" in described
            assert "model_name" in described
            assert _RETIRED_MODEL in described
            assert _SERVED_MODEL in described

    @pytest.mark.unit
    def test_overlay_filling_a_null_committed_field_is_not_a_shadow(self) -> None:
        """The bootstrap fallback working as designed must stay quiet.

        ``endpoint_url`` is null in the committed contract precisely so the
        site overlay can supply it. Flagging that would make the WARN
        meaningless on every standalone install.
        """
        provenance = build_overlay_field_provenance(
            _COMMITTED_BACKENDS,
            _STALE_OVERLAY["backends"],
            contract_source="contract.yaml",
            overlay_source="overlay.yaml",
        )

        endpoint = provenance.source_for("local-coder", "endpoint_url")
        assert endpoint is not None
        assert endpoint.source is EnumBifrostFieldSource.OVERLAY
        assert endpoint.shadowed_value is None
        assert endpoint.shadows_authoritative_field is False

    @pytest.mark.unit
    def test_committed_only_field_is_attributed_to_the_contract(self) -> None:
        provenance = build_overlay_field_provenance(
            _COMMITTED_BACKENDS,
            _STALE_OVERLAY["backends"],
            contract_source="contract.yaml",
            overlay_source="overlay.yaml",
        )

        tier = provenance.source_for("local-coder", "tier")
        assert tier is not None
        assert tier.source is EnumBifrostFieldSource.COMMITTED_CONTRACT
        assert tier.source_ref == "contract.yaml"

    @pytest.mark.unit
    def test_a_redundant_overlay_pin_is_still_a_shadow(self) -> None:
        """An overlay repeating today's committed value is tomorrow's stale pin.

        This is exactly how the 2026-09-18 overlay became a four-day-old lie
        without anybody editing it: it agreed with the contract until the
        contract moved.
        """
        agreeing_overlay = [
            {"backend_id": "local-coder", "model_name": _SERVED_MODEL},
        ]
        provenance = build_overlay_field_provenance(
            _COMMITTED_BACKENDS,
            agreeing_overlay,
            contract_source="contract.yaml",
            overlay_source="overlay.yaml",
        )

        shadows = provenance.shadows()
        assert len(shadows) == 1
        assert shadows[0].value == shadows[0].shadowed_value == _SERVED_MODEL


class TestOverlayShadowWarnsOnEveryLoad:
    """AC2 (log half): the override is logged at WARN with its path, every load."""

    @pytest.mark.unit
    def test_warn_names_the_overlay_path_the_key_and_both_values(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        provenance = build_overlay_field_provenance(
            _COMMITTED_BACKENDS,
            _STALE_OVERLAY["backends"],
            contract_source="src/omnimarket/configs/bifrost_delegation.yaml",
            overlay_source="/fixture-site/delegation/bifrost_overrides.yaml",
        )

        with caplog.at_level(logging.WARNING):
            warn_overlay_shadowed_authoritative_fields(provenance)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2
        blob = "\n".join(r.getMessage() for r in warnings)
        assert "/fixture-site/delegation/bifrost_overrides.yaml" in blob
        assert "backends[local-coder].model_name" in blob
        assert _RETIRED_MODEL in blob
        assert _SERVED_MODEL in blob

    @pytest.mark.unit
    def test_the_contract_loader_warns_on_every_load_not_only_the_first(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two loads, two sets of warnings.

        A once-per-process warning is indistinguishable from no warning to the
        operator who starts reading the log after boot.
        """
        contract, overlay = _write_pair(tmp_path)

        with caplog.at_level(logging.WARNING):
            load_bifrost_delegation_config(config_path=contract, overlay_path=overlay)
            first = len([r for r in caplog.records if _WARN_MARKER in r.getMessage()])
            load_bifrost_delegation_config(config_path=contract, overlay_path=overlay)
            second = len([r for r in caplog.records if _WARN_MARKER in r.getMessage()])

        assert first >= 2
        assert second == first * 2

    @pytest.mark.unit
    def test_no_overlay_means_no_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        contract, _overlay = _write_pair(tmp_path)
        absent = tmp_path / "does-not-exist.yaml"

        with caplog.at_level(logging.WARNING):
            load_bifrost_delegation_config(config_path=contract, overlay_path=absent)

        assert not [r for r in caplog.records if _WARN_MARKER in r.getMessage()]


class TestRoutingAuthorityCarriesTheSource:
    """AC1 (resolution half): the resolved backend knows where model_id came from."""

    @pytest.mark.unit
    def test_load_bifrost_backends_with_provenance_reports_the_file_overlay(
        self, tmp_path: Path
    ) -> None:
        contract, overlay = _write_pair(tmp_path)

        merged, provenance = load_bifrost_backends_with_provenance(
            config_path=contract, overlay_path=overlay
        )

        overridden = {
            b["backend_id"]: b["model_name"]
            for b in merged
            if b["backend_id"] in _OVERRIDDEN_BACKEND_IDS
        }
        assert overridden == dict.fromkeys(_OVERRIDDEN_BACKEND_IDS, _RETIRED_MODEL)
        assert provenance.overlay_source == str(overlay)
        assert {s.backend_id for s in provenance.shadows()} == set(
            _OVERRIDDEN_BACKEND_IDS
        )

    @pytest.mark.unit
    def test_resolved_backend_model_id_source_names_the_overlay_file_and_key(
        self, tmp_path: Path
    ) -> None:
        contract, overlay = _write_pair(tmp_path)

        resolved = resolve_delegation_backend(
            "codegen",
            backend_id="local-coder",
            config_path=contract,
            overlay_path=overlay,
        )

        assert resolved.model_id == _RETIRED_MODEL
        assert str(overlay) in resolved.model_id_source
        assert "backends[local-coder].model_name" in resolved.model_id_source
        assert _SERVED_MODEL in resolved.model_id_source

    @pytest.mark.unit
    def test_model_id_source_names_the_contract_when_no_overlay_overrides_it(
        self, tmp_path: Path
    ) -> None:
        contract, _stale = _write_pair(tmp_path)
        endpoint_only = tmp_path / "endpoint_only.yaml"
        endpoint_only.write_text(
            yaml.safe_dump(
                {
                    "backends": [
                        {
                            "backend_id": "local-coder",
                            "endpoint_url": "http://local-rung.invalid:8000/v1/chat/completions",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        resolved = resolve_delegation_backend(
            "codegen",
            backend_id="local-coder",
            config_path=contract,
            overlay_path=endpoint_only,
        )

        assert resolved.model_id == _SERVED_MODEL
        assert str(contract) in resolved.model_id_source
        assert str(endpoint_only) not in resolved.model_id_source


class TestRefusalNamesTheSource:
    """AC1 (refusal half): model_attribution_mismatch names file and key."""

    def _request(self, **overrides: Any) -> ModelLlmDelegationCallRequest:
        defaults: dict[str, Any] = {
            "request_id": "req-omn18670",
            "correlation_id": "corr-omn18670",
            "causation_id": "caus-omn18670",
            "model_id": _RETIRED_MODEL,
            "endpoint_ref": "http://local-rung.invalid:8000/v1/chat/completions",
            "prompt": "summarise this",
            "prompt_hash": "",
            "task_type": "research",
            "model_tier": "local",
            "provider": "local-coder",
            "timeout_seconds": 60.0,
        }
        defaults.update(overrides)
        return ModelLlmDelegationCallRequest(**defaults)

    @pytest.mark.unit
    def test_refusal_message_carries_the_overlay_path_and_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = (
            f"model_name={_RETIRED_MODEL!r} supplied by overlay "
            "/fixture-site/delegation/bifrost_overrides.yaml at "
            f"backends[local-coder].model_name, and SHADOWING {_SERVED_MODEL!r} "
            "declared by the tracked contract "
            "src/omnimarket/configs/bifrost_delegation.yaml"
        )
        monkeypatch.setattr(
            transport,
            "probe_served_models",
            lambda *_a, **_k: frozenset({_SERVED_MODEL}),
        )
        monkeypatch.setattr(
            transport,
            "post_chat_completion",
            lambda *_a, **_k: pytest.fail(
                "the fail-closed guard must refuse BEFORE any POST"
            ),
        )

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            result = HandlerLlmDelegationCall()(self._request(model_id_source=source))

        assert result.success is False
        assert (
            result.failure_class
            == EnumDelegationFailureClass.MODEL_ATTRIBUTION_MISMATCH
        )
        message = result.error_message or ""
        assert "bifrost_overrides.yaml" in message
        assert "backends[local-coder].model_name" in message
        assert _SERVED_MODEL in message

    @pytest.mark.unit
    def test_guard_still_fails_closed_when_no_source_is_known(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unattributed mismatch still refuses — the guard is not relaxed.

        Provenance is added to the message; it is never a precondition of the
        refusal. A caller that resolved a backend outside the routing
        authority carries no source, and must still be refused.
        """
        monkeypatch.setattr(
            transport,
            "probe_served_models",
            lambda *_a, **_k: frozenset({_SERVED_MODEL}),
        )
        monkeypatch.setattr(
            transport,
            "post_chat_completion",
            lambda *_a, **_k: pytest.fail("must not POST on a mismatch"),
        )

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            result = HandlerLlmDelegationCall()(self._request())

        assert result.success is False
        assert (
            result.failure_class
            == EnumDelegationFailureClass.MODEL_ATTRIBUTION_MISMATCH
        )


class TestExplainRouting:
    """AC3: the explain surface prints the merged routing with a per-key source."""

    @pytest.mark.unit
    def test_explain_prints_a_source_for_every_key_and_flags_the_shadow(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from omnimarket.cli.cli_explain_routing import main

        contract, overlay = _write_pair(tmp_path)

        exit_code = main(
            ["--contract-path", str(contract), "--overlay-path", str(overlay)]
        )
        out = capsys.readouterr().out

        assert exit_code == 0
        assert str(contract) in out
        assert str(overlay) in out
        # per-key source, both directions
        assert "model_name" in out
        assert "endpoint_url" in out
        assert "committed_contract" in out
        assert "overlay" in out
        # the shadow is called out, with both values
        assert _RETIRED_MODEL in out
        assert _SERVED_MODEL in out
        assert "backends[local-coder].model_name" in out

    @pytest.mark.unit
    def test_explain_exit_code_flags_a_shadow_when_asked(self, tmp_path: Path) -> None:
        """``--fail-on-shadow`` makes the explain surface usable as a probe."""
        from omnimarket.cli.cli_explain_routing import main

        contract, overlay = _write_pair(tmp_path)

        assert (
            main(
                [
                    "--contract-path",
                    str(contract),
                    "--overlay-path",
                    str(overlay),
                    "--fail-on-shadow",
                ]
            )
            == 1
        )

    @pytest.mark.unit
    def test_explain_is_clean_when_the_overlay_only_fills_null_fields(
        self, tmp_path: Path
    ) -> None:
        from omnimarket.cli.cli_explain_routing import main

        contract, _stale = _write_pair(tmp_path)
        endpoint_only = tmp_path / "endpoint_only.yaml"
        endpoint_only.write_text(
            yaml.safe_dump(
                {
                    "backends": [
                        {
                            "backend_id": "local-coder",
                            "endpoint_url": "http://local-rung.invalid:8000/v1/chat/completions",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        assert (
            main(
                [
                    "--contract-path",
                    str(contract),
                    "--overlay-path",
                    str(endpoint_only),
                    "--fail-on-shadow",
                ]
            )
            == 0
        )
