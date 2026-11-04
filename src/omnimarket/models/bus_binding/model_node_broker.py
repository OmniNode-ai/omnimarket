# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The broker a node's bus caller dials, as the node's contract overlay resolves it (OMN-22362)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class ModelNodeBroker(BaseModel):
    """What the overlay resolves for one node: the bus kind and, for a broker, how to dial it.

    ``kafka`` carries the lane's bootstrap address and its client transport; ``inmemory`` carries
    neither. The SASL password is a ``SecretStr`` so it never reaches a repr, a log line or an error
    message; :meth:`client_security` is the one place it is revealed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["kafka", "inmemory"]
    overlay_path: Path | None = Field(
        default=None, description="The overlay file the declaration was read from."
    )
    lane: str | None = None
    bootstrap_servers: str | None = Field(default=None, min_length=1)
    security_protocol: str | None = None
    sasl_mechanism: str | None = None
    sasl_username: str | None = None
    sasl_password: SecretStr | None = None

    @model_validator(mode="after")
    def _transport_matches_kind(self) -> Self:
        if self.kind == "kafka":
            if not self.bootstrap_servers:
                raise ValueError("a kafka broker needs bootstrap_servers")
        elif any(
            value is not None
            for value in (
                self.lane,
                self.bootstrap_servers,
                self.security_protocol,
                self.sasl_mechanism,
                self.sasl_username,
                self.sasl_password,
            )
        ):
            raise ValueError(
                "an inmemory bus has no broker, so it carries no transport"
            )
        return self

    def client_security(self) -> dict[str, str]:
        """The librdkafka security settings for this broker; empty for a plaintext lane."""
        config: dict[str, str] = {}
        if self.security_protocol:
            config["security.protocol"] = self.security_protocol
        if self.sasl_mechanism:
            config["sasl.mechanism"] = self.sasl_mechanism
        if self.sasl_username:
            config["sasl.username"] = self.sasl_username
        if self.sasl_password is not None:
            config["sasl.password"] = self.sasl_password.get_secret_value()
        return config


__all__ = ["ModelNodeBroker"]
