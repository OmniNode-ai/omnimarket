# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O for the dev-tag pin mover (OMN-19998)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumConsumerTestOutcome(StrEnum):
    """The consumer's test run against the candidate pin."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"


class EnumPinMoveRefusal(StrEnum):
    """Why a move was refused. No PR is opened for any of these."""

    NOT_A_DEV_TAG = "not_a_dev_tag"
    NO_TAG_PIN = "no_tag_pin"
    NOT_FORWARD = "not_forward"
    CONSUMER_TESTS_NOT_RUN = "consumer_tests_not_run"
    CONSUMER_TESTS_FAILED = "consumer_tests_failed"


class ModelDevTagPinMoveRequest(BaseModel):
    """A consumer's pyproject.toml text and the sibling's new dev tag."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pyproject_text: str
    distribution: str
    repository_url: str
    new_tag: str
    consumer_tests: EnumConsumerTestOutcome = EnumConsumerTestOutcome.NOT_RUN


class ModelDevTagPinMoveResult(BaseModel):
    """The moved text when accepted, a typed refusal otherwise.

    ``candidate_text`` is the rewritten text whenever the rewrite itself is
    possible, so the caller can lock and test the consumer on it; only an
    accepted result (the consumer's tests passed on the candidate) is opened
    as a PR.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    refusal: EnumPinMoveRefusal | None = None
    detail: str = ""
    current_tag: str | None = None
    new_tag: str | None = None
    candidate_text: str = ""
