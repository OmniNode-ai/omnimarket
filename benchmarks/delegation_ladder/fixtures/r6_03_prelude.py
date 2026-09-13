"""Adapted from omnimarket
src/omnimarket/nodes/node_projection_delegation/handlers/handler_projection_delegation.py
at the commit immediately BEFORE 60b188edf272f3ef568e6fb5e30ed50b5c4eb150. The database
adapter and the projection write are in-module stubs; the dispatch shim below is the
merged pre-fix text."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DatabaseAdapter:
    """Stub standing in for the real adapter; only its type is load-bearing here."""


class ModelDelegationJudgeVerdictEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delegation_id: str
    verdict: str
    score: float
