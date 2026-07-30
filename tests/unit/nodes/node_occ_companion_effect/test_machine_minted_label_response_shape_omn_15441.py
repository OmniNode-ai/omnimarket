# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``occ:machine-minted`` label POST decodes an ARRAY response (OMN-15441).

``POST /repos/{owner}/{repo}/issues/{n}/labels`` responds with the issue's full
label ARRAY. Routing it through ``rest_json`` — whose contract is dict-only —
made every otherwise-successful call raise
``unexpected JSON response type for /repos/.../labels``.

The blast radius came from the swallow: ``_apply_machine_minted_label`` is
non-fatal by design, so the label was silently never applied and
``minted_by_node`` lost its only marker while the job reported success. Live:
run 30496115784 logged exactly that warning against OCC#5516.

This is a response-SHAPE defect, NOT a permission one — it is unrelated to the
product-body 403 in the same run, which is why it gets its own test module.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from typing import Any
from unittest.mock import patch

import pytest

from omnimarket.events.occ_autoauthor import OCC_MACHINE_MINTED_LABEL
from omnimarket.github_api import GitHubApiError, rest_json_array
from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    HandlerOccCompanionEffect,
)

_MOD = (
    "omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect"
)

# The real GitHub response body for the labels POST: an array of label objects.
_LABELS_ARRAY_RESPONSE: list[dict[str, Any]] = [
    {"id": 1, "name": OCC_MACHINE_MINTED_LABEL},
    {"id": 2, "name": "evidence"},
]


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@pytest.mark.unit
class TestLabelPostDecodesArrayResponse:
    def test_rest_json_array_accepts_a_body_and_decodes_the_array(self) -> None:
        """The helper must be able to POST a body, not only GET lists.

        RED before the fix: ``rest_json_array`` had no ``body`` parameter at
        all, so the labels POST had nowhere to go except ``rest_json``.
        """
        captured: dict[str, Any] = {}

        def _fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            captured["method"] = req.get_method()
            captured["data"] = req.data
            captured["content_type"] = req.get_header("Content-type")
            return _FakeResponse(_LABELS_ARRAY_RESPONSE)

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = rest_json_array(
                "POST",
                "/repos/OmniNode-ai/onex_change_control/issues/5516/labels",
                token="tok",
                body={"labels": [OCC_MACHINE_MINTED_LABEL]},
            )

        assert result == _LABELS_ARRAY_RESPONSE
        assert captured["method"] == "POST"
        assert json.loads(captured["data"]) == {"labels": [OCC_MACHINE_MINTED_LABEL]}
        assert captured["content_type"] == "application/json"

    def test_body_is_omitted_entirely_when_not_supplied(self) -> None:
        """Existing GET callers must not start sending an empty body."""
        captured: dict[str, Any] = {}

        def _fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            captured["data"] = req.data
            captured["content_type"] = req.get_header("Content-type")
            return _FakeResponse([])

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            rest_json_array("GET", "/repos/o/r/pulls", token="tok")

        assert captured["data"] is None
        assert captured["content_type"] is None

    def test_handler_label_apply_succeeds_and_logs_no_warning(self, caplog) -> None:
        """The real handler path applies the label without the shape warning.

        RED before the fix: ``rest_json`` raised on the array response and the
        handler logged "could not apply 'occ:machine-minted' label".
        """
        handler = HandlerOccCompanionEffect.__new__(HandlerOccCompanionEffect)

        def _fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            return _FakeResponse(_LABELS_ARRAY_RESPONSE)

        with (
            caplog.at_level(logging.WARNING, logger=_MOD),
            patch("urllib.request.urlopen", side_effect=_fake_urlopen),
        ):
            handler._apply_machine_minted_label(
                "OmniNode-ai", "onex_change_control", 5516, "tok"
            )

        assert "could not apply" not in caplog.text

    def test_handler_still_swallows_a_real_api_failure(self, caplog) -> None:
        """The non-fatal contract is preserved — a 500 must not abort authoring."""
        handler = HandlerOccCompanionEffect.__new__(HandlerOccCompanionEffect)

        def _boom(req, timeout=None):  # type: ignore[no-untyped-def]
            raise urllib.error.HTTPError(
                req.full_url,
                500,
                "Server Error",
                {},
                None,  # type: ignore[arg-type]
            )

        with (
            caplog.at_level(logging.WARNING, logger=_MOD),
            patch("urllib.request.urlopen", side_effect=_boom),
        ):
            handler._apply_machine_minted_label(
                "OmniNode-ai", "onex_change_control", 5516, "tok"
            )

        assert "could not apply" in caplog.text

    def test_rest_json_still_rejects_an_array_response(self) -> None:
        """The dict-only contract on ``rest_json`` is unchanged, not loosened.

        The fix routes the array-returning endpoint to the right helper; it does
        NOT weaken ``rest_json`` into accepting either shape.
        """
        from omnimarket.github_api import rest_json

        def _fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            return _FakeResponse(_LABELS_ARRAY_RESPONSE)

        with (
            patch("urllib.request.urlopen", side_effect=_fake_urlopen),
            pytest.raises(GitHubApiError, match="unexpected JSON response type"),
        ):
            rest_json("POST", "/repos/o/r/issues/1/labels", token="tok")
