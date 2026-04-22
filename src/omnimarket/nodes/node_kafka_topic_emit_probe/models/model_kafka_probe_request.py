"""ModelKafkaProbeRequest — input model for the Kafka topic emit probe node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelKafkaProbeRequest(BaseModel):
    """Input command for the Kafka topic emit probe handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topics: list[str] = Field(
        default_factory=list, description="Kafka topics to probe."
    )
    probe_interval_seconds: int = Field(
        default=3600, description="Interval between probes in seconds."
    )
    verify_consumers: bool = Field(
        default=True, description="Verify consumer group advancement."
    )
    handler_name: str = Field(
        default="handler_kafka_probe", description="Handler name for routing."
    )


__all__ = ["ModelKafkaProbeRequest"]
