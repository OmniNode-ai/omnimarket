# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Both bifrost overlay merge paths agree on overlay-only ``backend_id``s (OMN-16903).

omnimarket has two code paths that merge a site overlay onto the committed
``src/omnimarket/configs/bifrost_delegation.yaml``. Before this ticket they
behaved OPPOSITELY on the same input — an overlay entry whose ``backend_id``
the committed contract does not declare:

    * ``adapters/llm/bifrost/config_loader_bifrost_delegation.py`` →
      ``deep_merge_bifrost_delegation_config`` / ``_merge_lists`` **appended**
      it as a new ``backends[N]`` entry. Site overlays are hand-written and
      carry only ``backend_id`` / ``endpoint_url`` / ``model_name``, so the
      appended partial entry then failed ``ModelBifrostDelegationConfig``
      validation and the ENTIRE config load raised — taking every task type
      down, not just the retired one, with a pydantic ``backends.11.tier``
      message that points at a list INDEX rather than naming the culprit.
    * ``routing/delegation_backend_resolution.py`` → ``_merge_overlay``
      **silently dropped** it (it iterates only the committed list), narrowing
      the routing table with no signal at all.

Chosen semantic (ticket option 1, "fail loud but attributably"): both paths
REJECT an overlay-only ``backend_id`` with an error that names the offending
id AND the overlay source it came from. This preserves fail-fast (CLAUDE.md
rule 8) while making a stale site overlay diagnosable from the message alone.

Merging the two loaders into one is explicitly OUT OF SCOPE for OMN-16903.

Related:
    - OMN-16903: this ticket (the two paths disagreed)
    - OMN-16442: discovered the divergence while retiring ``local-reasoner``
    - OMN-15155: the property-3 test retargeted onto the unified behaviour
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    OverlayOnlyBackendIdError,
    load_bifrost_delegation_config,
)
from omnimarket.routing.delegation_backend_resolution import (
    BIFROST_OVERLAY_STORE_KEY,
    load_bifrost_backends,
)

_BIFROST_CONFIG_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)

#: A backend_id the committed contract does NOT declare — the exact shape of a
#: stale site overlay row left behind by a backend retirement (OMN-16442).
_RETIRED_BACKEND_ID = "local-reasoner"

#: A backend_id the committed contract DOES declare, with a null endpoint_url
#: that the overlay is expected to supply (the legitimate overlay use case).
_COMMITTED_BACKEND_ID = "local-coder"

#: A COMPLETE chat-completions URL (never a bare ``/v1`` base, OMN-12815). A
#: non-routable ``.example`` host, not a lab IP — these tests never open a
#: socket, and a literal lab address in a test file is itself a violation
#: (``test_no_hardcoded_literals_in_tests``).
_COMPLETE_URL = "http://lane-a.example:8000/v1/chat/completions"


class _MockStore:
    """Minimal in-memory ProtocolSecretStore for unit tests."""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    async def get_secret(self, key: str) -> str | None:
        return self._data.get(key)


def _overlay_yaml(backends: list[dict[str, Any]]) -> str:
    return yaml.safe_dump({"backends": backends}, sort_keys=False)


def _write_overlay(tmp_path: Path, backends: list[dict[str, Any]]) -> Path:
    overlay_path = tmp_path / "bifrost_overrides.yaml"
    overlay_path.write_text(_overlay_yaml(backends), encoding="utf-8")
    return overlay_path


def _stale_overlay_row() -> dict[str, Any]:
    """A hand-written site overlay row for a retired backend.

    Deliberately partial — ``backend_id`` / ``endpoint_url`` / ``model_name``
    only, with no ``tier`` / ``timeout_ms`` / ``max_tokens``. This is what real
    site overlays look like, and it is why the appending path produced a whole
    config validation failure rather than a merely-unroutable entry.
    """
    return {
        "backend_id": _RETIRED_BACKEND_ID,
        "endpoint_url": _COMPLETE_URL,
        "model_name": "qwen3-reasoner",
    }


def _assert_attributable(exc: OverlayOnlyBackendIdError, source_fragment: str) -> None:
    """The error must name the offending id AND its source, not a list index."""
    message = str(exc)
    assert _RETIRED_BACKEND_ID in message, (
        f"the error must NAME the offending overlay-only backend_id; got: {message!r}"
    )
    assert source_fragment in message, (
        "the error must attribute the offending row to its overlay SOURCE "
        f"(expected {source_fragment!r} in the message); got: {message!r}"
    )
    assert "backends." not in message, (
        "the error must not degrade into the pre-OMN-16903 pydantic "
        f"'backends.N.tier' index message; got: {message!r}"
    )


# ---------------------------------------------------------------------------
# 1. The appending path (config_loader_bifrost_delegation) now rejects.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_appending_loader_rejects_overlay_only_backend_id(tmp_path: Path) -> None:
    """``load_bifrost_delegation_config`` must reject, not append-then-explode.

    Pre-OMN-16903 this raised ``ValueError`` from pydantic naming
    ``backends.11.tier`` — an index into a merged list the operator never
    wrote, with no mention of ``local-reasoner`` or the overlay file.
    """
    overlay_path = _write_overlay(tmp_path, [_stale_overlay_row()])

    with pytest.raises(OverlayOnlyBackendIdError) as excinfo:
        load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay_path)

    _assert_attributable(excinfo.value, str(overlay_path))


# ---------------------------------------------------------------------------
# 2. The dropping path (delegation_backend_resolution) now rejects too.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolution_loader_rejects_overlay_only_backend_id_from_file(
    tmp_path: Path,
) -> None:
    """``load_bifrost_backends`` must reject rather than silently drop.

    Pre-OMN-16903 the stale row vanished with no log line and no exception:
    the routing table quietly narrowed.
    """
    overlay_path = _write_overlay(tmp_path, [_stale_overlay_row()])

    with pytest.raises(OverlayOnlyBackendIdError) as excinfo:
        load_bifrost_backends(
            config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
        )

    _assert_attributable(excinfo.value, str(overlay_path))


@pytest.mark.unit
def test_resolution_loader_rejects_overlay_only_backend_id_from_store() -> None:
    """The store overlay is attributed by its STORE KEY, not a filesystem path."""
    store = _MockStore(
        {BIFROST_OVERLAY_STORE_KEY: _overlay_yaml([_stale_overlay_row()])}
    )

    with pytest.raises(OverlayOnlyBackendIdError) as excinfo:
        load_bifrost_backends(config_path=_BIFROST_CONFIG_PATH, store=store)

    _assert_attributable(excinfo.value, BIFROST_OVERLAY_STORE_KEY)


# ---------------------------------------------------------------------------
# 3. The parity property itself: same input, same outcome, both paths.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_both_merge_paths_agree_on_overlay_only_backend_id(tmp_path: Path) -> None:
    """The OMN-16903 invariant, stated directly.

    One input class (an overlay row naming a backend_id the committed contract
    does not declare) must produce ONE outcome, whichever loader a caller
    happens to reach for. Before this ticket the blast radius of retiring a
    backend_id depended entirely on that accident.
    """
    overlay_path = _write_overlay(tmp_path, [_stale_overlay_row()])

    with pytest.raises(OverlayOnlyBackendIdError) as appending:
        load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay_path)

    with pytest.raises(OverlayOnlyBackendIdError) as dropping:
        load_bifrost_backends(
            config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
        )

    assert type(appending.value) is type(dropping.value)
    for exc in (appending.value, dropping.value):
        _assert_attributable(exc, str(overlay_path))


@pytest.mark.unit
def test_all_offending_backend_ids_are_named_not_just_the_first(
    tmp_path: Path,
) -> None:
    """A stale overlay usually carries MORE than one retired row.

    Naming only the first would force the operator through one edit-reload
    cycle per stale entry.
    """
    second_retired = "local-coder-mlx"
    overlay_path = _write_overlay(
        tmp_path,
        [
            _stale_overlay_row(),
            {"backend_id": second_retired, "endpoint_url": _COMPLETE_URL},
        ],
    )

    with pytest.raises(OverlayOnlyBackendIdError) as excinfo:
        load_bifrost_backends(
            config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
        )

    message = str(excinfo.value)
    assert _RETIRED_BACKEND_ID in message
    assert second_retired in message


# ---------------------------------------------------------------------------
# 4. Negative control: the LEGITIMATE overlay use case must still work.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_overlay_row_for_a_committed_backend_id_still_merges_on_both_paths(
    tmp_path: Path,
) -> None:
    """The whole point of the overlay — supplying a site-local COMPLETE URL for
    a committed backend whose repo default endpoint_url is null — is untouched.
    """
    overlay_path = _write_overlay(
        tmp_path,
        [{"backend_id": _COMMITTED_BACKEND_ID, "endpoint_url": _COMPLETE_URL}],
    )

    config = load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay_path)
    by_id = {b.backend_id: b for b in config.backends}
    assert by_id[_COMMITTED_BACKEND_ID].endpoint_url == _COMPLETE_URL

    backends = load_bifrost_backends(
        config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
    )
    resolved = {b["backend_id"]: b for b in backends}
    assert resolved[_COMMITTED_BACKEND_ID]["endpoint_url"] == _COMPLETE_URL


@pytest.mark.unit
def test_absent_overlay_is_not_treated_as_an_overlay_only_declaration(
    tmp_path: Path,
) -> None:
    """No overlay file at all must remain a clean load on both paths."""
    missing = tmp_path / "does-not-exist.yaml"

    config = load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, missing)
    assert config.backends

    backends = load_bifrost_backends(
        config_path=_BIFROST_CONFIG_PATH, overlay_path=missing
    )
    assert backends


@pytest.mark.unit
def test_empty_overlay_backends_list_is_not_an_overlay_only_declaration(
    tmp_path: Path,
) -> None:
    """An overlay that declares no backends at all is a no-op, not a rejection."""
    overlay_path = tmp_path / "bifrost_overrides.yaml"
    overlay_path.write_text(
        textwrap.dedent(
            """\
            backends: []
            """
        ),
        encoding="utf-8",
    )

    config = load_bifrost_delegation_config(_BIFROST_CONFIG_PATH, overlay_path)
    assert config.backends

    backends = load_bifrost_backends(
        config_path=_BIFROST_CONFIG_PATH, overlay_path=overlay_path
    )
    assert backends
