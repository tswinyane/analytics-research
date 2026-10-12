from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from app.schemas import HarmonizedPublication


class BaseConnector(ABC):
    source_name: str

    @abstractmethod
    def fetch(
        self,
        limit: int = 100,
        state: dict | None = None,
    ) -> Iterable[HarmonizedPublication]:
        raise NotImplementedError

    def build_next_state(
        self,
        publications: list[HarmonizedPublication],
        previous_state: dict | None = None,
    ) -> dict | None:
        return previous_state
