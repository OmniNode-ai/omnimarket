# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A site overlay may ADD a backend, when the entry validates as complete (OMN-17099).

Operator ruling 2026-09-22 ("remove the block"): OMN-16903 refused every overlay
entry naming a ``backend_id`` the committed contract does not declare. Its
reason was real — hand-written overlay rows were partial, and an appended
partial row failed whole-config validation with a pydantic list-index message —
but a blanket refusal made the overlay unable to register a backend at all.

The replacement is contract validation. An overlay entry that ADDS a backend is
accepted when it validates on its own as a complete
``ModelDelegationBackendConfig`` and explicitly carries every field the
routing authority would otherwise have to default: ``provider``, a COMPLETE
``endpoint_url``, ``model_name``, ``tier``, ``timeout_ms``, ``max_tokens`` and
the ``secret_ref`` key (a string ref, or null meaning an explicit none). A
partial entry is refused with ``OverlayBackendIncompleteError`` naming the
backend_id, the overlay source and the missing fields — never silently dropped,
never defaulted. Both merge paths share the one rule, so one input class still
produces one outcome whichever loader a caller reaches for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.adapters.llm.bifrost import config_loader_bifrost_delegation
from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    OverlayBackendIncompleteError,
    ProviderSurfaceMismatchError,
    build_overlay_field_provenance,
    load_bifrost_delegation_config,
    load_bifrost_delegation_config_payload,
)
from omnimarket.models.delegation.model_bifrost_overlay_provenance import (
    EnumBifrostFieldSource,
)
from omnimarket.routing.delegation_backend_resolution import (
    BIFROST_OVERLAY_STORE_KEY,
    load_bifrost_backends,
    load_bifrost_backends_with_provenance,
)

pytestmark = pytest.mark.unit

_BIFROST_CONFIG_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)

#: Non-routable ``.example`` hosts: these tests never open a socket.
_ADDED_URL = "https://inference.site-added.example/v1/chat/completions"
_SECOND_ADDED_URL = "https://second.site-added.example/v1/chat/completions"


class _MockStore:
    """Minimal in-memory ProtocolSecretStore for unit tests."""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    async def get_secret(self, key: str) -> str | None:
        return self._data.get(key)


def _complete_entry(
    backend_id: str = "site-added-coder", **overrides: Any
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "backend_id": backend_id,
        "provider": "site-inference",
        "endpoint_url": _ADDED_URL,
        "model_name": "site-model-1",
        "tier": "cheap_cloud",
        "timeout_ms": 60000,
        "max_tokens": 8192,
        "secret_ref": "llm.site_added.api_key",
        "capabilities": ["code_generation"],
    }
    entry.update(overrides)
    return entry


def _write_overlay(tmp_path: Path, backends: list[dict[str, Any]]) -> Path:
    overlay_path = tmp_path / "bifrost_overrides.yaml"
    overlay_path.write_text(
        yaml.safe_dump({"backends": backends}, sort_keys=False), encoding="utf-8"
    )
    return overlay_path


def _committed_ids() -> list[str]:
    raw = yaml.safe_load(_BIFROST_CONFIG_PATH.read_text(encoding="utf-8"))
    return [b["backend_id"] for b in raw["backends"]]


# ---------------------------------------------------------------------------
# 1. The old blanket refusal is gone, by name.
# ---------------------------------------------------------------------------


def test_the_blanket_overlay_only_refusal_no_longer_exists() -> None:
    """No alias of the OMN-16903 names survives the rename."""
    assert not hasattr(
        config_loader_bifrost_delegation, "reject_overlay_only_backend_ids"
    )
    assert not hasattr(config_loader_bifrost_delegation, "OverlayOnlyBackendIdError")
    assert (
        "reject_overlay_only_backend_ids"
        not in config_loader_bifrost_delegation.__all__
    )
    assert "OverlayOnlyBackendIdError" not in config_loader_bifrost_delegation.__all__
    assert "OverlayBackendIncompleteError" in config_loader_bifrost_delegation.__all__


# ---------------------------------------------------------------------------
# 2. A complete added backend is accepted on both paths, appended in order.
# ---------------------------------------------------------------------------


def test_both_paths_append_complete_added_backends_in_overlay_order(
    tmp_path: Path,
) -> None:
    second = _complete_entry(
        "site-added-reasoner",
        endpoint_url=_SECOND_ADDED_URL,
        secret_ref=None,
    )
    overlay_path = _write_overlay(tmp_path, [_complete_entry(), second])
    committed = _committed_ids()

    config = load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay_path)
    ids = [b.backend_id for b in config.backends]
    assert ids == [*committed, "site-added-coder", "site-added-reasoner"]
    added = {b.backend_id: b for b in config.backends}["site-added-coder"]
    assert added.endpoint_url == _ADDED_URL
    assert added.secret_ref == "llm.site_added.api_key"

    backends = load_bifrost_backends(
        config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
    )
    assert [b["backend_id"] for b in backends] == [
        *committed,
        "site-added-coder",
        "site-added-reasoner",
    ]
    resolved = {b["backend_id"]: b for b in backends}
    assert resolved["site-added-coder"]["endpoint_url"] == _ADDED_URL
    assert resolved["site-added-reasoner"]["secret_ref"] is None


def test_store_overlay_may_add_a_complete_backend() -> None:
    store = _MockStore(
        {
            BIFROST_OVERLAY_STORE_KEY: yaml.safe_dump(
                {"backends": [_complete_entry()]}, sort_keys=False
            )
        }
    )
    backends = load_bifrost_backends(config_path=_BIFROST_CONFIG_PATH, store=store)
    assert backends[-1]["backend_id"] == "site-added-coder"
    assert backends[-1]["model_name"] == "site-model-1"


def test_payload_loader_accepts_a_complete_added_backend() -> None:
    config = load_bifrost_delegation_config_payload(
        _BIFROST_CONFIG_PATH.read_bytes(),
        yaml.safe_dump({"backends": [_complete_entry()]}).encode(),
        contract_source="contract.yaml",
        overlay_source="overlay.yaml",
    )
    assert config.backends[-1].backend_id == "site-added-coder"


def test_explicit_null_secret_ref_is_accepted(tmp_path: Path) -> None:
    overlay_path = _write_overlay(tmp_path, [_complete_entry(secret_ref=None)])
    config = load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay_path)
    assert config.backends[-1].secret_ref is None


# ---------------------------------------------------------------------------
# 3. A partial added entry is refused attributably, on both paths.
# ---------------------------------------------------------------------------


def _assert_names(exc: OverlayBackendIncompleteError, *fragments: str) -> None:
    message = str(exc)
    for fragment in fragments:
        assert fragment in message, f"expected {fragment!r} in: {message!r}"
    assert "backends." not in message, (
        "must not degrade to the pydantic list-index message OMN-16903 removed"
    )


@pytest.mark.parametrize(
    "missing_field",
    [
        "provider",
        "endpoint_url",
        "model_name",
        "tier",
        "timeout_ms",
        "max_tokens",
        "secret_ref",
    ],
)
def test_missing_required_field_is_refused_on_both_paths(
    tmp_path: Path, missing_field: str
) -> None:
    entry = _complete_entry()
    del entry[missing_field]
    overlay_path = _write_overlay(tmp_path, [entry])

    with pytest.raises(OverlayBackendIncompleteError) as appending:
        load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay_path)
    with pytest.raises(OverlayBackendIncompleteError) as merging:
        load_bifrost_backends(
            config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
        )

    for exc in (appending.value, merging.value):
        _assert_names(exc, "site-added-coder", str(overlay_path), missing_field)
        assert exc.overlay_source == str(overlay_path)
        assert missing_field in exc.missing_fields_by_backend["site-added-coder"]


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_required_string_is_refused(tmp_path: Path, blank: str | None) -> None:
    overlay_path = _write_overlay(tmp_path, [_complete_entry(model_name=blank)])
    with pytest.raises(OverlayBackendIncompleteError) as excinfo:
        load_bifrost_backends(
            config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
        )
    _assert_names(excinfo.value, "site-added-coder", "model_name")


def test_blank_secret_ref_string_is_refused(tmp_path: Path) -> None:
    """Null is an explicit none; an empty string is neither a ref nor a none."""
    overlay_path = _write_overlay(tmp_path, [_complete_entry(secret_ref="  ")])
    with pytest.raises(OverlayBackendIncompleteError) as excinfo:
        load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay_path)
    _assert_names(excinfo.value, "site-added-coder", "secret_ref")


@pytest.mark.parametrize(
    "url",
    [
        "https://inference.site-added.example/v1",
        "https://inference.site-added.example",
        "https://inference.site-added.example/",
        "inference.site-added.example/v1/chat/completions",
        "ftp://inference.site-added.example/v1/chat/completions",
    ],
)
def test_incomplete_endpoint_url_is_refused(tmp_path: Path, url: str) -> None:
    overlay_path = _write_overlay(tmp_path, [_complete_entry(endpoint_url=url)])
    with pytest.raises(OverlayBackendIncompleteError) as excinfo:
        load_bifrost_backends(
            config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
        )
    _assert_names(excinfo.value, "site-added-coder", "endpoint_url")


def test_stale_partial_row_is_refused_naming_every_missing_field(
    tmp_path: Path,
) -> None:
    """The exact OMN-16903 input — a hand-written three-key row — still refuses."""
    overlay_path = _write_overlay(
        tmp_path,
        [
            {
                "backend_id": "local-reasoner",
                "endpoint_url": "http://lane-a.example:8000/v1/chat/completions",
                "model_name": "qwen3-reasoner",
            }
        ],
    )
    with pytest.raises(OverlayBackendIncompleteError) as excinfo:
        load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay_path)
    missing = excinfo.value.missing_fields_by_backend["local-reasoner"]
    assert set(missing) >= {
        "provider",
        "tier",
        "timeout_ms",
        "max_tokens",
        "secret_ref",
    }


def test_every_incomplete_entry_is_named_not_just_the_first(tmp_path: Path) -> None:
    overlay_path = _write_overlay(
        tmp_path,
        [
            {"backend_id": "retired-one", "endpoint_url": _ADDED_URL},
            {"backend_id": "retired-two", "model_name": "x"},
        ],
    )
    with pytest.raises(OverlayBackendIncompleteError) as excinfo:
        load_bifrost_backends(
            config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
        )
    _assert_names(excinfo.value, "retired-one", "retired-two")


def test_entry_without_a_backend_id_is_refused(tmp_path: Path) -> None:
    entry = _complete_entry()
    del entry["backend_id"]
    overlay_path = _write_overlay(tmp_path, [entry])
    with pytest.raises(OverlayBackendIncompleteError) as appending:
        load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay_path)
    with pytest.raises(OverlayBackendIncompleteError) as merging:
        load_bifrost_backends(
            config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
        )
    for exc in (appending.value, merging.value):
        _assert_names(exc, "backend_id", str(overlay_path))


def test_entry_failing_the_backend_model_is_refused(tmp_path: Path) -> None:
    """Complete on the named fields but invalid for the model: still refused."""
    overlay_path = _write_overlay(
        tmp_path, [_complete_entry(timeout_ms=5, not_a_field="x")]
    )
    with pytest.raises(OverlayBackendIncompleteError) as excinfo:
        load_bifrost_backends(
            config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
        )
    _assert_names(excinfo.value, "site-added-coder", "timeout_ms", "not_a_field")


def test_committed_backend_partial_override_is_unchanged(tmp_path: Path) -> None:
    """Rows naming a COMMITTED backend stay field-by-field overrides."""
    overlay_path = _write_overlay(
        tmp_path,
        [{"backend_id": "local-coder", "endpoint_url": _ADDED_URL}],
    )
    config = load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay_path)
    by_id = {b.backend_id: b for b in config.backends}
    assert by_id["local-coder"].endpoint_url == _ADDED_URL
    assert [b.backend_id for b in config.backends] == _committed_ids()


# ---------------------------------------------------------------------------
# 4. The OMN-17314 provider-surface check covers added entries too.
# ---------------------------------------------------------------------------


def test_added_backend_on_a_wrong_provider_surface_is_refused_on_both_paths(
    tmp_path: Path,
) -> None:
    overlay_path = _write_overlay(
        tmp_path,
        [_complete_entry(endpoint_url="https://api.z.ai/api/paas/v4/chat/completions")],
    )
    with pytest.raises(ProviderSurfaceMismatchError):
        load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay_path)
    with pytest.raises(ProviderSurfaceMismatchError) as excinfo:
        load_bifrost_backends(
            config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
        )
    assert "site-added-coder" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5. The OMN-18670 provenance record covers added entries.
# ---------------------------------------------------------------------------


def test_provenance_records_every_field_of_an_added_backend(tmp_path: Path) -> None:
    overlay_path = _write_overlay(tmp_path, [_complete_entry()])
    _, provenance = load_bifrost_backends_with_provenance(
        config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
    )
    for field_name in ("provider", "endpoint_url", "model_name", "tier", "secret_ref"):
        record = provenance.source_for("site-added-coder", field_name)
        assert record is not None, f"no provenance for added field {field_name}"
        assert record.source is EnumBifrostFieldSource.OVERLAY
        assert record.source_ref == str(overlay_path)
        assert record.shadowed_value is None
    assert not [s for s in provenance.shadows() if s.backend_id == "site-added-coder"]


def test_build_overlay_field_provenance_orders_added_after_committed() -> None:
    provenance = build_overlay_field_provenance(
        [{"backend_id": "committed-one", "tier": "local"}],
        [_complete_entry()],
        contract_source="contract.yaml",
        overlay_source="overlay.yaml",
    )
    backend_ids = [f.backend_id for f in provenance.fields]
    assert backend_ids[0] == "committed-one"
    assert backend_ids[-1] == "site-added-coder"
    secret = provenance.source_for("site-added-coder", "secret_ref")
    assert secret is not None
    assert secret.value == "llm.site_added.api_key"
