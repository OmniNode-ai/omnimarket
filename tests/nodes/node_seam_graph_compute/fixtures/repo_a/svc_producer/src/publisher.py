"""Fixture source file exercising the code-level seam extractors.

Not real dispatch code — a minimal, ruff-clean stand-in shaped exactly like
a Kafka producer/consumer call site so the extractor's regex-based scanners
have something legitimate to match.
"""

import os


class _FakeKafkaProducer:
    def send(self, topic: str, payload: object) -> None:
        del topic, payload


class _FakeKafkaConsumer:
    def subscribe(self, topics: list[str]) -> None:
        del topics


producer = _FakeKafkaProducer()
consumer = _FakeKafkaConsumer()
payload = {"example": True}

producer.send("tenant-x.onex.evt.example-produced.v1", payload)


def consume() -> None:
    consumer.subscribe(["tenant-x.onex.cmd.example-consumed.v1"])


_EP = os.environ["FIXTURE_ENDPOINT_URL"]  # node-purity-ok: fixture, OMN-15763

# @ref: configs/service_endpoints.yaml#backends.cloud-gemini-pro
