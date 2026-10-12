from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HarvestRequest(BaseModel):
    sources: list[str] | None = None
    limit: int = Field(default=100, ge=0, le=100000)
    article_only: bool = False
    reset_state: bool = False


class HealthResponse(BaseModel):
    status: Literal["ok"]
    application: str
