# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_review_finding_veto_effect (OMN-17427).

The hostile reviewer's voters are language models. Two of them agreed that a
test file "is syntactically invalid Python" and blocked a pull request on it,
and ``ast.parse`` of that file at that head succeeded. A claim that a file does
not parse is checkable without a model, so this handler checks it: a blocking
finding that says a Python, YAML or JSON file is invalid is verified by parsing
that file at the pull request head, and dropped, with its reason, when it parses.

Only that one class is touched. A finding whose title is not a syntax claim,
names another file or language than the one it is reported against, or is about
a string parsed at run time stays as the reviewers raised it; so does a claim
whose file does not parse or cannot be read at the head. Non-blocking findings
are never examined.
"""

from __future__ import annotations

import ast
import copy
import json
import logging
import re
from typing import Any

import yaml

from omnimarket.nodes.node_review_finding_veto_effect.handlers.adapter_git_head import (
    GitHeadFileReader,
)
from omnimarket.nodes.node_review_finding_veto_effect.models import (
    ModelFindingVeto,
    ModelReviewFindingVetoRequest,
    ModelReviewFindingVetoResult,
)
from omnimarket.nodes.node_review_finding_veto_effect.protocols import (
    ProtocolHeadFileReader,
)

_log = logging.getLogger(__name__)

_TITLE_SEPARATOR = " | "

# A title that says the file as a whole does not parse.
_SYNTAX_CLAIM = re.compile(
    r"syntactically\s+(?:in|im)valid"
    r"|\bsyntax\s+errors?\b"
    r"|\bsyntaxerror\b"
    r"|\binvalid\s+(?:python|yaml|json|syntax)\b"
    r"|\bnot\s+(?:a\s+)?valid\s+(?:python|yaml|json)\b"
    r"|\b(?:malformed|broken)\s+(?:python|yaml|json)\b"
    r"|\b(?:does|do|will|would)\s*(?:not|n't)\s+(?:parse|compile)\b"
    r"|\bfails?\s+to\s+(?:parse|compile)\b"
    r"|\bcannot\s+be\s+(?:parsed|compiled)\b"
    r"|\bunparseable\b",
    re.IGNORECASE,
)

# A title about text the code parses while it runs is not a claim about the file.
_RUNTIME_PARSE = re.compile(
    r"\beval\b|\bexec\b|\bcompile\s*\(|ast\.parse|literal_eval|json\.loads"
    r"|yaml\.(?:safe_)?load|\bat\s+runtime\b",
    re.IGNORECASE,
)

_LANGUAGE_WORDS = re.compile(r"\b(python|yaml|json)\b", re.IGNORECASE)
_FILE_TOKEN = re.compile(r"[\w./-]+\.(?:py|ya?ml|json)\b", re.IGNORECASE)

_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
}


def _title(message: str) -> str:
    return message.split(_TITLE_SEPARATOR, 1)[0]


def _target_path(file_path: str) -> str:
    """The reviewer writes a model's free-text location into the path field."""
    return file_path.split(":", 1)[0].strip()


def _language_of(path: str) -> str | None:
    lowered = path.lower()
    for suffix, language in _LANGUAGE_BY_SUFFIX.items():
        if lowered.endswith(suffix):
            return language
    return None


def _parses(language: str, text: str) -> bool:
    try:
        if language == "python":
            ast.parse(text)
        elif language == "yaml":
            list(yaml.safe_load_all(text))
        else:
            json.loads(text)
    except (SyntaxError, ValueError, yaml.YAMLError, RecursionError):
        return False
    return True


class HandlerReviewFindingVeto:
    """EFFECT: verifies syntax claims in blocking findings against the head's files."""

    def __init__(self, reader: ProtocolHeadFileReader | None = None) -> None:
        self._reader = reader or GitHeadFileReader(".")

    def handle(
        self, request: ModelReviewFindingVetoRequest
    ) -> ModelReviewFindingVetoResult:
        document = copy.deepcopy(request.result)
        quorum = document.get("quorum")
        if not isinstance(quorum, dict):
            return ModelReviewFindingVetoResult(result=document)
        clusters = quorum.get("blocking_findings")
        if not isinstance(clusters, list) or not clusters:
            return ModelReviewFindingVetoResult(result=document)

        raw_messages = _raw_messages(document)
        kept: list[Any] = []
        vetoed: list[ModelFindingVeto] = []
        unverified: list[str] = []
        for cluster in clusters:
            outcome = self._judge(cluster, raw_messages, request.head_sha)
            if isinstance(outcome, ModelFindingVeto):
                vetoed.append(outcome)
                _log.info("review finding vetoed: %s", outcome.reason)
                continue
            if outcome is not None:
                unverified.append(outcome)
                _log.info("review syntax claim kept: %s", outcome)
            kept.append(cluster)

        if not vetoed:
            return ModelReviewFindingVetoResult(
                result=document, kept_unverified=tuple(unverified)
            )

        quorum["blocking_findings"] = kept
        quorum["blocking_count"] = len(kept)
        quorum["vetoed_findings"] = [v.model_dump() for v in vetoed]
        if not kept and quorum.get("verdict") == "blocked":
            quorum["verdict"] = "passed"
        return ModelReviewFindingVetoResult(
            result=document, vetoed=tuple(vetoed), kept_unverified=tuple(unverified)
        )

    def _judge(
        self,
        cluster: Any,
        raw_messages: dict[str, str],
        head_sha: str,
    ) -> ModelFindingVeto | str | None:
        """A veto when the claim is disproved, a reason string when a claim stays
        blocking, None when the finding is not a syntax claim at all."""
        if not isinstance(cluster, dict):
            return None
        message = str(cluster.get("message", ""))
        messages = [message] + [
            raw_messages[str(i)]
            for i in cluster.get("finding_ids") or []
            if str(i) in raw_messages
        ]
        if not all(_SYNTAX_CLAIM.search(_title(m)) for m in messages):
            return None
        path = _target_path(str(cluster.get("file_path", "")))
        label = f"{path}: {_title(message)[:80]}"
        if any(_RUNTIME_PARSE.search(_title(m)) for m in messages):
            return f"{label} (about text parsed at run time)"
        language = _language_of(path)
        if language is None:
            return f"{label} (not a python, yaml or json file)"
        if not self._claim_is_about(path, language, messages):
            return f"{label} (names another file or language)"
        text = self._reader.read(head_sha, path)
        if text is None:
            return f"{label} (file not readable at {head_sha})"
        if not _parses(language, text):
            return f"{label} (file does not parse at {head_sha})"
        return ModelFindingVeto(
            file_path=path,
            line_start=int(cluster.get("line_start", 1) or 1),
            language=language,
            message=message,
            reason=(
                f"{path} parses as {language} at {head_sha}; the finding claiming "
                f"it is invalid was dropped"
            ),
        )

    @staticmethod
    def _claim_is_about(path: str, language: str, messages: list[str]) -> bool:
        basename = path.rsplit("/", 1)[-1].lower()
        for message in messages:
            for word in _LANGUAGE_WORDS.findall(_title(message)):
                if word.lower() != language:
                    return False
            for token in _FILE_TOKEN.findall(_title(message)):
                if token.rsplit("/", 1)[-1].lower() != basename:
                    return False
        return True


def _raw_messages(document: dict[str, Any]) -> dict[str, str]:
    """Each voter's own message for every finding id the clusters cite."""
    messages: dict[str, str] = {}
    for result in document.get("results") or []:
        if not isinstance(result, dict):
            continue
        for finding in result.get("findings") or []:
            if isinstance(finding, dict) and "finding_id" in finding:
                messages[str(finding["finding_id"])] = str(
                    finding.get("normalized_message", "")
                )
    return messages


__all__: list[str] = ["HandlerReviewFindingVeto"]
