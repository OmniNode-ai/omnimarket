# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded fake transport for node_pr_landing_github_effect (OMN-19826).

Replays the scenarios under ``tests/fixtures/pr_landing/github/*.json``. Each
scenario holds one command and the ordered HTTP exchanges it produces. The fake
is strict: a request that differs from the recorded one in method, path, body
or conditional header raises instead of answering, so a request-shape drift in
the node's seam fails the replay rather than passing on a loose match.

Fixture provenance is part of the data. ``recorded`` scenarios came from real
read-only calls; ``documented`` scenarios (every mutation) follow GitHub's
documented response shapes, because recording them would need a mutating call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubFailureReason,
    EnumPrLandingGithubOperation,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "pr_landing" / "github"
)


class ModelFixtureExchange(BaseModel):
    """One recorded request and the response GitHub gave to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelGithubHttpRequest
    response: ModelGithubHttpResponse


class ModelFixtureExpected(BaseModel):
    """What the scenario must classify to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["completed", "failed"]
    failure_reason: EnumPrLandingGithubFailureReason | None
    not_modified: bool
    retry_after_seconds: int | None = None


class ModelFixturePrecondition(BaseModel):
    """A recorded read that establishes a fact the scenario depends on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provenance: Literal["recorded"]
    recorded_at: str
    note: str
    request: ModelGithubHttpRequest
    response: ModelGithubHttpResponse


class ModelFixtureScenario(BaseModel):
    """One replayable scenario file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str
    operation: EnumPrLandingGithubOperation
    provenance: Literal["recorded", "documented"]
    provenance_note: str = Field(min_length=1)
    recorded_at: str | None
    precondition_evidence: ModelFixturePrecondition | None = None
    command: dict[str, object]
    expected: ModelFixtureExpected
    exchanges: tuple[ModelFixtureExchange, ...] = Field(min_length=1)


class FakeTransportMismatchError(AssertionError):
    """A request did not match the next recorded exchange."""


def load_scenario(name: str, fixture_dir: Path = FIXTURE_DIR) -> ModelFixtureScenario:
    path = fixture_dir / f"{name}.json"
    return ModelFixtureScenario.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )


def all_scenarios(fixture_dir: Path = FIXTURE_DIR) -> tuple[ModelFixtureScenario, ...]:
    return tuple(
        load_scenario(p.stem, fixture_dir) for p in sorted(fixture_dir.glob("*.json"))
    )


class FakeGithubLandingTransport:
    """Strict, ordered replay of one scenario's recorded exchanges.

    Implements ``ProtocolPrLandingGithubTransport``. ``sent`` records every
    request the caller made, so a dry-run test can assert it stays empty.
    """

    def __init__(self, scenario: ModelFixtureScenario) -> None:
        self._scenario = scenario
        self._pending = list(scenario.exchanges)
        self.sent: list[ModelGithubHttpRequest] = []

    @classmethod
    def for_scenario(cls, name: str) -> FakeGithubLandingTransport:
        return cls(load_scenario(name))

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        if not self._pending:
            raise FakeTransportMismatchError(
                f"{self._scenario.scenario}: no recorded exchange left for {request!r}"
            )
        exchange = self._pending.pop(0)
        if request != exchange.request:
            raise FakeTransportMismatchError(
                f"{self._scenario.scenario}: request {request!r} does not match "
                f"the recorded request {exchange.request!r}"
            )
        return exchange.response

    def assert_drained(self) -> None:
        if self._pending:
            raise FakeTransportMismatchError(
                f"{self._scenario.scenario}: {len(self._pending)} recorded "
                "exchange(s) were never requested"
            )
