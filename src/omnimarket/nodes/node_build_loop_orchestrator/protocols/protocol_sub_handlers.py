from abc import ABC, abstractmethod


class SubHandlerProtocol(ABC):
    @abstractmethod
    def handle(self, data):
        pass