"""Adoption recovery effect entry point, bound to its handler by contract."""

from collections.abc import Callable
from pathlib import Path

from .handler import HandlerAdoptionRecovery
from .models import ModelAdoptionRecoveryRequest


class NodeAdoptionRecoveryEffect:
    """Invoke the journal and detached-process effect with an injected launcher."""

    def __init__(self, start: Callable[[Path], str | None]) -> None:
        self.handler = HandlerAdoptionRecovery(start)

    def handle(
        self, request: ModelAdoptionRecoveryRequest
    ) -> list[tuple[str, str, str, str]]:
        return self.handler.handle(request)
