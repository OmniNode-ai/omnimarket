# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: each refusal is typed and names the backends it concerns (OMN-22774)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_media_capability_resolve_compute.handlers import (
    handler_media_capability_resolve as handler_module,
)
from omnimarket.nodes.node_media_capability_resolve_compute.models import (
    EnumMediaCapabilityRefusal,
    ModelMediaCapabilityRequest,
)
from tests.nodes.node_media_capability_resolve_compute.test_golden_chain_media_capability_resolve import (
    _BY_CAPABILITY,
    _HOST,
    backend,
)

_HANDLER = handler_module.HandlerMediaCapabilityResolve()


@pytest.mark.parametrize(
    ("capability", "wrong_path"),
    [
        ("tts", "/v1/audio/transcriptions"),
        ("stt", "/v1/audio/speech"),
        ("embed", "/v1/chat/completions"),
        ("decide", "/v1/embeddings"),
        ("video", "/v1/chat/completions"),
    ],
)
def test_error_capability_and_endpoint_shape_disagree(
    capability: str, wrong_path: str
) -> None:
    backend_id, _, port = _BY_CAPABILITY[capability]
    request = ModelMediaCapabilityRequest(
        capability=capability,
        backends=(backend(capability, endpoint_url=f"{_HOST}:{port}{wrong_path}"),),
    )

    result = _HANDLER.handle(request)

    assert result.status == "refused"
    assert result.refusal is EnumMediaCapabilityRefusal.SHAPE_DISAGREES
    assert result.named_backends == (backend_id,)


def test_error_two_serving_backends_for_one_capability() -> None:
    request = ModelMediaCapabilityRequest(
        capability="stt",
        backends=(backend("stt"), backend("stt", backend_id="media-stt-2")),
    )

    result = _HANDLER.handle(request)

    assert result.refusal is EnumMediaCapabilityRefusal.DUPLICATE_SERVING
    assert result.named_backends == ("media-stt", "media-stt-2")


def test_error_unknown_capability_name() -> None:
    request = ModelMediaCapabilityRequest(capability="sing", backends=(backend("tts"),))

    result = _HANDLER.handle(request)

    assert result.refusal is EnumMediaCapabilityRefusal.UNKNOWN_CAPABILITY
    assert "sing" in result.detail


def test_error_capability_asked_with_none_serving_names_the_dark_backend() -> None:
    request = ModelMediaCapabilityRequest(
        capability="video", backends=(backend("video", serving=False),)
    )

    result = _HANDLER.handle(request)

    assert result.refusal is EnumMediaCapabilityRefusal.NONE_SERVING
    assert result.named_backends == ("media-video",)


def test_error_capability_asked_with_no_backend_declared() -> None:
    request = ModelMediaCapabilityRequest(capability="stt", backends=(backend("tts"),))

    result = _HANDLER.handle(request)

    assert result.refusal is EnumMediaCapabilityRefusal.NONE_SERVING
    assert result.named_backends == ()


def test_failing_control_with_the_shape_check_removed_a_speech_backend_filed_under_transcription_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injected break: with the shape check removed, the disagreement is no longer refused.

    A node asking for speech would then be handed the transcription URL. The disagreement test
    above reads refused on the real handler and resolved on this one, so it reads the check.
    """
    request = ModelMediaCapabilityRequest(
        capability="tts",
        backends=(
            backend("tts", endpoint_url=f"{_HOST}:8102/v1/audio/transcriptions"),
        ),
    )
    assert _HANDLER.handle(request).status == "refused"

    monkeypatch.setattr(handler_module, "_shape_disagrees", lambda _backend: False)

    broken = _HANDLER.handle(request)
    assert broken.status == "resolved"
    assert broken.endpoint_url.endswith("/v1/audio/transcriptions")
