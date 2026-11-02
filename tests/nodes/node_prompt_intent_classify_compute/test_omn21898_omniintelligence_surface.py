# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The prompt-intent handler calls the omniintelligence classifier API it really exports (OMN-21898).

On 2026-11-01 every dispatch of ``content-captured.v1`` raised ``ImportError:
cannot import name 'classify_intent'`` on the dev runtime: the handler imported
a module-level function that the installed omniintelligence does not define
(it defines ``HandlerIntentClassifier`` and ``HandlerTypedClassification``).
The runtime terminalized each failure onto the node's success topic, and the
intent-classification projection dead-lettered every one of them.

omniintelligence is a runtime peer and is not a dependency of omnimarket, so CI
cannot import it. The ``recorded`` surface below is a stand-in package carrying
exactly the exports read from the dev runtime image (no ``classify_intent``);
the handler's import statements run against it unchanged. The ``real`` surface
is added whenever omniintelligence is importable, for a lab run with its
``src`` on ``PYTHONPATH``.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from collections.abc import Iterator

import pytest
from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_prompt_intent_classify_compute.handlers import (
    handler_prompt_intent_classify as handler_module,
)

_NODE = "omniintelligence.nodes.node_intent_classifier_compute"
_SURFACES = ["recorded"] + (
    ["real"] if importlib.util.find_spec("omniintelligence") is not None else []
)


class _ClassifyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    content: str


class _ResolveRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    intent_category: str
    confidence: float


class _IntentClass:
    value = "BUGFIX"


class _TypedIntent:
    intent_class = _IntentClass()


class _HandlerIntentClassifier:
    def handle(self, request: _ClassifyRequest) -> dict[str, object]:
        return {
            "intent_category": "debugging",
            "confidence": 1.0,
            "keywords": ["fix"],
            "all_scores": {"debugging": 1.0},
        }


class _HandlerIntentClassification:
    """Present in the installed module; not what this handler should call."""


class _HandlerTypedClassification:
    def handle(self, request: _ResolveRequest) -> _TypedIntent:
        assert request.intent_category == "debugging"
        return _TypedIntent()


def _module(name: str, **attrs: object) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = []
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


@pytest.fixture(params=_SURFACES)
def surface(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    handler_module._omniintelligence_functions.cache_clear()
    if request.param == "recorded":
        modules = {
            "omniintelligence": _module("omniintelligence"),
            "omniintelligence.nodes": _module("omniintelligence.nodes"),
            _NODE: _module(_NODE),
            f"{_NODE}.handlers": _module(f"{_NODE}.handlers"),
            f"{_NODE}.handlers.handler_intent_classification": _module(
                f"{_NODE}.handlers.handler_intent_classification",
                HandlerIntentClassifier=_HandlerIntentClassifier,
                HandlerIntentClassification=_HandlerIntentClassification,
            ),
            f"{_NODE}.handlers.handler_typed_classification": _module(
                f"{_NODE}.handlers.handler_typed_classification",
                HandlerTypedClassification=_HandlerTypedClassification,
            ),
            f"{_NODE}.models": _module(
                f"{_NODE}.models",
                ModelClassifyIntentRequest=_ClassifyRequest,
                ModelResolveTypedIntentRequest=_ResolveRequest,
            ),
        }
        for name, mod in modules.items():
            monkeypatch.setitem(sys.modules, name, mod)
    yield request.param
    handler_module._omniintelligence_functions.cache_clear()


@pytest.mark.unit
def test_classifier_import_resolves_against_installed_surface(surface: str) -> None:
    handler_module._omniintelligence_functions()


@pytest.mark.unit
def test_classify_with_omniintelligence_returns_a_verdict(surface: str) -> None:
    verdict = handler_module.classify_with_omniintelligence(
        "fix the failing test, it throws a traceback"
    )

    assert verdict.intent_category == "debugging"
    assert verdict.intent_class == "BUGFIX"
    assert verdict.confidence == 1.0
    assert "fix" in verdict.keywords
