# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for delegation-path config provenance (OMN-12967)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.inference.delegation_config_provenance import (
    DELEGATION_PATH_CONFIG_KEYS,
    LOADER_PACKAGED_DEFAULT,
    EnumDelegationConfigSource,
    ModelDelegationConfigProvenance,
    resolve_optional_path_config,
    resolve_path_config,
)

pytestmark = pytest.mark.unit


def test_override_present_resolves_to_contract_overlay_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELEGATION_TEST_PATH", "/etc/onex/overlay.yaml")
    bootstrap = Path("/packaged/default.yaml")

    resolved, provenance = resolve_path_config("DELEGATION_TEST_PATH", bootstrap)

    assert resolved == Path("/etc/onex/overlay.yaml")
    assert provenance.source is EnumDelegationConfigSource.CONTRACT_OVERLAY_ENV
    assert provenance.override_present is True
    assert provenance.resolved_path == Path("/etc/onex/overlay.yaml")
    assert provenance.config_key == "DELEGATION_TEST_PATH"


def test_override_absent_falls_back_to_bootstrap_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DELEGATION_TEST_PATH", raising=False)
    bootstrap = Path("/packaged/default.yaml")

    resolved, provenance = resolve_path_config("DELEGATION_TEST_PATH", bootstrap)

    assert resolved == bootstrap
    assert provenance.source is EnumDelegationConfigSource.BOOTSTRAP_DEFAULT
    assert provenance.override_present is False
    assert provenance.resolved_path == bootstrap


def test_blank_override_is_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DELEGATION_TEST_PATH", "   ")
    bootstrap = Path("/packaged/default.yaml")

    resolved, provenance = resolve_path_config("DELEGATION_TEST_PATH", bootstrap)

    assert resolved == bootstrap
    assert provenance.source is EnumDelegationConfigSource.BOOTSTRAP_DEFAULT
    assert provenance.override_present is False


def test_provenance_line_is_emitted_at_info(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DELEGATION_TEST_PATH", "/etc/onex/overlay.yaml")
    bootstrap = Path("/packaged/default.yaml")

    with caplog.at_level(
        logging.INFO,
        logger="omnimarket.inference.delegation_config_provenance",
    ):
        resolve_path_config("DELEGATION_TEST_PATH", bootstrap)

    lines = [r.message for r in caplog.records]
    assert any(
        "config_provenance surface=delegation" in line
        and "config_key=DELEGATION_TEST_PATH" in line
        and "source=contract_overlay_env" in line
        and "override_present=true" in line
        for line in lines
    )


def test_log_line_for_bootstrap_default_is_serializable() -> None:
    provenance = ModelDelegationConfigProvenance(
        config_key="BIFROST_CONTRACT_PATH",
        source=EnumDelegationConfigSource.BOOTSTRAP_DEFAULT,
        resolved_path=Path("<bifrost-loader-packaged-default>"),
        override_present=False,
    )

    line = provenance.log_line()

    assert "source=bootstrap_default" in line
    assert "override_present=false" in line
    assert "config_key=BIFROST_CONTRACT_PATH" in line


def test_provenance_model_is_frozen() -> None:
    provenance = ModelDelegationConfigProvenance(
        config_key="K",
        source=EnumDelegationConfigSource.BOOTSTRAP_DEFAULT,
        resolved_path=Path("/p"),
        override_present=False,
    )

    with pytest.raises(ValidationError):
        provenance.config_key = "other"  # type: ignore[misc]


def test_optional_override_present_returns_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", "/etc/onex/bifrost.yaml")

    resolved, provenance = resolve_optional_path_config("BIFROST_CONTRACT_PATH")

    assert resolved == Path("/etc/onex/bifrost.yaml")
    assert provenance.source is EnumDelegationConfigSource.CONTRACT_OVERLAY_ENV
    assert provenance.override_present is True
    assert provenance.resolved_path == Path("/etc/onex/bifrost.yaml")


def test_optional_override_absent_returns_none_with_loader_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)

    resolved, provenance = resolve_optional_path_config("BIFROST_CONTRACT_PATH")

    assert resolved is None
    assert provenance.source is EnumDelegationConfigSource.BOOTSTRAP_DEFAULT
    assert provenance.override_present is False
    assert provenance.resolved_path == LOADER_PACKAGED_DEFAULT


def test_optional_blank_override_is_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", "   ")

    resolved, provenance = resolve_optional_path_config("BIFROST_OVERLAY_PATH")

    assert resolved is None
    assert provenance.source is EnumDelegationConfigSource.BOOTSTRAP_DEFAULT
    assert provenance.override_present is False


def test_optional_provenance_line_emitted_at_info(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)

    with caplog.at_level(
        logging.INFO,
        logger="omnimarket.inference.delegation_config_provenance",
    ):
        resolve_optional_path_config("BIFROST_CONTRACT_PATH")

    lines = [r.message for r in caplog.records]
    assert any(
        "config_provenance surface=delegation" in line
        and "config_key=BIFROST_CONTRACT_PATH" in line
        and "source=bootstrap_default" in line
        and "override_present=false" in line
        for line in lines
    )


def test_delegation_path_config_keys_cover_known_routing_path_reads() -> None:
    # These are the path-config keys re-homed onto the provenance resolver. The
    # delegation-env scanner ratchet enforces raw reads of these keys outside the
    # provenance module are violations even with a skip token.
    assert (
        frozenset(
            {
                "BIFROST_CONTRACT_PATH",
                "BIFROST_OVERLAY_PATH",
                "DELEGATION_ROUTING_TIERS_PATH",
                "TASK_CLASS_CONTRACT_PATH",
                "INFERENCE_PROTOCOL_CONFIG_PATH",
            }
        )
        == DELEGATION_PATH_CONFIG_KEYS
    )
