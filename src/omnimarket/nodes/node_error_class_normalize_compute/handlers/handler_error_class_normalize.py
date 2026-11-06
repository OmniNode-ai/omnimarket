# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure error-class computation; every deployment fact is in the request."""

import hashlib
import json
import re
from typing import Literal

from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorClassNormalizeResult,
    ModelErrorNormalizerFacts,
)


def _component(value: str) -> str:
    # Preserve punctuation in producer codes without allowing key separators or
    # collisions introduced by slugification. The slug in the result is cosmetic.
    return "".join(
        char
        if char.isascii() and (char.isalnum() or char in "-_")
        else f"%{ord(char):x};"
        for char in value
    )


def _strip_text(text: str, facts: ModelErrorNormalizerFacts) -> str:
    template = text
    for stage in facts.configuration.strip_stages:
        patterns: tuple[str, ...]
        if stage.kind == "hosts":
            # Longest first avoids a shorter registered host eating the
            # beginning of a longer name. Boundaries exclude embedded names.
            hosts = sorted(set(facts.hosts), key=lambda host: (-len(host), host))
            if any(not host for host in hosts):
                raise ValueError("host facts must contain nonempty names")
            patterns = (
                (r"(?<![\w.-])(?:" + "|".join(map(re.escape, hosts)) + r")(?![\w.-])",)
                if hosts
                else ()
            )
        elif stage.kind == "lanes":
            patterns = facts.configuration.lane_name_patterns
        else:
            if not stage.pattern:
                raise ValueError("regex stage requires a nonempty pattern")
            patterns = (stage.pattern,)
        for pattern in patterns:

            def substitute(
                _: re.Match[str], replacement: str = stage.replacement
            ) -> str:
                return replacement

            template = re.sub(pattern, substitute, template)
    template = " ".join(template.split())
    return template


class HandlerErrorClassNormalize:
    def handle(
        self, request: ModelErrorClassNormalizeRequest
    ) -> ModelErrorClassNormalizeResult:
        facts = request.facts
        configuration = facts.configuration
        encoded = json.dumps(
            facts.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        version = hashlib.sha256(encoded).hexdigest()
        if request.key_version is not None and request.key_version != version:
            raise ValueError("key_version does not match supplied normalization facts")

        event = request.event
        adapters = [
            item
            for item in configuration.producer_adapters
            if item.source == event.source and item.producer in (event.producer, "*")
        ]
        exact = [item for item in adapters if item.producer == event.producer]
        candidates = exact or adapters
        if len(candidates) != 1:
            raise ValueError("producer must resolve to exactly one configured adapter")
        adapter = candidates[0]
        template = _strip_text(event.text, facts)
        digest = hashlib.sha256(template.encode()).hexdigest()[:10]
        provisional = event.text_kind == "prose"
        identity = event.code if adapter.unique_codes and event.code else "template"
        if provisional:
            key = f"prose.{_component(event.producer)}.{digest}"
            identity = "provisional"
        else:
            key = ".".join(
                (
                    _component(event.source),
                    _component(event.producer),
                    _component(identity),
                    digest,
                )
            )
        slug = re.sub(r"[^a-z0-9]+", "-", template.lower()).strip("-")[:64]
        canonical: str | None = key
        alias_state: Literal["not_provisional", "unresolved", "canonical", "none"] = (
            "not_provisional"
        )
        if provisional:
            canonical = None
            alias_state = "unresolved"
            matches = [
                alias for alias in request.aliases if alias.provisional_key == key
            ]
            if len(matches) > 1:
                raise ValueError(
                    "a provisional key must have at most one triage resolution"
                )
            if matches:
                canonical = matches[0].canonical_key
                alias_state = "canonical" if canonical is not None else "none"
        return ModelErrorClassNormalizeResult(
            class_key=key,
            key_version=version,
            template=template,
            slug=slug,
            provisional=provisional,
            identity=identity,
            canonical_key=canonical,
            alias_state=alias_state,
        )
