# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rows of the code graph's ``handlers`` question (OMN-26337).

One row is one operation of one contract's ``handler_routing``: the handler class and module, the
input and output model, the topics, the declared failure causes and the declared examples, each
reference resolved against the version's own definition index. A reference the index cannot place
is named in ``unresolved`` with the reason, and an operation with no observable route is NO_ROUTE;
neither is ever dropped.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumHandlerResolution(StrEnum):
    """Whether every reference of a row resolved to a definition of the version."""

    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"


class EnumHandlerRoute(StrEnum):
    """How an event reaches the operation."""

    #: The contract's ``runtime_dispatch.command_topic``.
    COMMAND_TOPIC = "COMMAND_TOPIC"
    #: A subscribed topic, for an operation that has an observable output.
    SUBSCRIBE_TOPIC = "SUBSCRIBE_TOPIC"
    #: Neither a command topic nor a subscribed topic with an observable output.
    NO_ROUTE = "NO_ROUTE"


class EnumHandlerReference(StrEnum):
    """Which reference of a row an unresolved entry is about."""

    HANDLER = "handler"
    INPUT_MODEL = "input_model"
    OUTPUT_MODEL = "output_model"


class EnumUnresolvedReason(StrEnum):
    """Why the definition index could not place a reference."""

    #: The row declares no such reference at all.
    NOT_DECLARED = "not-declared"
    #: The module's top-level package is not one this version defines.
    MODULE_OUTSIDE_REPOSITORY = "module-outside-repository"
    #: No file of this version is the module.
    MODULE_NOT_FOUND = "module-not-found"
    #: The module is a file of this version and defines no such class.
    CLASS_NOT_DEFINED = "class-not-defined"


class ModelUnresolvedReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: EnumHandlerReference
    #: The dotted reference as the contract wrote it (``module.Class``), or empty when not declared.
    declared: str
    reason: EnumUnresolvedReason


class ModelHandlerExample(BaseModel):
    """One example a contract declares for an operation: an input and, optionally, its output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    example_id: str = Field(min_length=1)
    payload: dict[str, object]
    expected_output: dict[str, object] | None = None


class ModelHandlerRow(BaseModel):
    """One operation of one contract, read from the contract and resolved against the version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The repository-relative path of the contract file.
    contract: str = Field(min_length=1)
    #: The node's identity as the contract import names it.
    node: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    handler_class: str = ""
    handler_module: str = ""
    #: The file that defines the handler class, when it resolved.
    handler_path: str | None = None
    input_model: str = ""
    input_model_path: str | None = None
    output_model: str = ""
    output_model_path: str | None = None
    route: EnumHandlerRoute
    command_topic: str | None = None
    subscribe_topics: tuple[str, ...] = ()
    publish_topics: tuple[str, ...] = ()
    terminal_event: str | None = None
    failure_causes: tuple[str, ...] = ()
    examples: tuple[ModelHandlerExample, ...] = ()
    resolution: EnumHandlerResolution
    unresolved: tuple[ModelUnresolvedReference, ...] = ()
    #: Why the IR import refused the contract, when it did: the row then carries no IR topics.
    contract_import_error: str | None = None
    #: The commit the row was read at.
    revision: str = Field(min_length=1)
