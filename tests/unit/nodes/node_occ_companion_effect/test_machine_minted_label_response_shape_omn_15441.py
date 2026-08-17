# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``occ:machine-minted`` label POST decodes an ARRAY response (OMN-15441).

``POST /repos/{owner}/{repo}/issues/{n}/labels`` responds with the issue's full
label ARRAY. Routing it through ``rest_json`` — whose contract is dict-only —
made every otherwise-successful call raise
``unexpected JSON response type for /repos/.../labels``.

The blast radius is narrower than the raise suggests, and the distinction is
load-bearing: **the label still landed.** ``rest_json`` calls ``urlopen`` and
only raises afterwards, on ``isinstance(parsed, dict)`` — by then the POST has
committed server-side and GitHub has applied the label. Live corroboration: run
30496115784 logged "could not apply ... unexpected JSON response type" against
OCC#5516, yet OCC#5516 carries ``occ:machine-minted``, applied by
``onexbot-occ-writer[bot]`` at 2026-07-29T22:26:20Z (issue-events API); peers
OCC#5526/#5528/#5530 the same.

So the real defect is a spurious swallowed WARNING that falsely reports a lost
provenance marker — it misleads operators and any log-scraping audit, but
``minted_by_node`` never actually lost its marker.
``test_the_shape_error_is_raised_after_the_post_has_committed`` pins that
ordering so the claim cannot silently invert again.

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

    def test_a_persistent_500_is_retried_then_fails_authoring(self) -> None:
        """OMN-16071: `ci:ready` is CI-gating now, so the old swallow is gone.

        A 500 is a transient transport shape (``_is_transient_github_error``),
        so ``call_with_retry`` retries it up to ``_RETRY_MAX_ATTEMPTS`` before
        re-raising — the exception then propagates out of
        ``_apply_machine_minted_label`` and fails the authoring operation
        rather than silently reporting a companion as authored while it
        carries only the marker label.
        """
        handler = HandlerOccCompanionEffect.__new__(HandlerOccCompanionEffect)
        attempts = 0

        def _boom(req, timeout=None):  # type: ignore[no-untyped-def]
            nonlocal attempts
            attempts += 1
            raise urllib.error.HTTPError(
                req.full_url,
                500,
                "Server Error",
                {},
                None,  # type: ignore[arg-type]
            )

        with (
            patch("urllib.request.urlopen", side_effect=_boom),
            patch("omnimarket.occ_git_transport.time.sleep"),
            pytest.raises(GitHubApiError),
        ):
            handler._apply_machine_minted_label(
                "OmniNode-ai", "onex_change_control", 5516, "tok"
            )

        assert attempts == 3  # _RETRY_MAX_ATTEMPTS, all exhausted

    def test_the_shape_error_is_raised_after_the_post_has_committed(self) -> None:
        """The pre-fix defect lost the response DECODE, never the label itself.

        Pins the corrected impact claim. ``rest_json`` raises on the array only
        after ``urlopen`` has returned, so the mutation is already committed
        server-side when the error surfaces — which is why OCC#5516 carries
        ``occ:machine-minted`` (applied by ``onexbot-occ-writer[bot]`` at
        2026-07-29T22:26:20Z) despite its own run logging "could not apply".

        This test exists because an earlier revision of this fix asserted the
        label "was never once applied", which live evidence disproves. If the
        helper is ever reordered to validate the response shape BEFORE issuing
        the request, this fails — and at that point the stronger claim would
        become true and the docstrings above would need to change with it.
        """
        from omnimarket.github_api import rest_json

        requests_issued: list[str] = []

        def _fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            requests_issued.append(f"{req.get_method()} {req.full_url}")
            return _FakeResponse(_LABELS_ARRAY_RESPONSE)

        with (
            patch("urllib.request.urlopen", side_effect=_fake_urlopen),
            pytest.raises(GitHubApiError, match="unexpected JSON response type"),
        ):
            rest_json(
                "POST",
                "/repos/OmniNode-ai/onex_change_control/issues/5516/labels",
                token="tok",
                body={"labels": [OCC_MACHINE_MINTED_LABEL]},
            )

        # The request WAS sent before the decode raised: the label landed.
        assert requests_issued == [
            "POST https://api.github.com/repos/OmniNode-ai/onex_change_control"
            "/issues/5516/labels"
        ]

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
