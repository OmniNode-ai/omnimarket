from pydantic import BaseModel, ConfigDict, Field


class ModelGoldenChainDescriptor(BaseModel):
    """A single head->consumer->tail chain the GOLDEN_CHAIN probe asserts.

    Strongly typed so the orchestrator never threads loose dicts of topic /
    group / table strings through the probe boundary (OMN-13145).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chain_name: str = Field(..., description="Human-readable golden-chain identifier.")
    command_topic: str = Field(
        ..., description="Head command topic that drives the chain."
    )
    consumer_group: str = Field(
        ..., description="Consumer group that must be registered on the lane."
    )
    tail_database: str = Field(
        ..., description="Postgres database that owns the chain's tail table."
    )
    tail_table: str = Field(
        ...,
        description="Tail projection table; existence + row presence completes the chain.",
    )
