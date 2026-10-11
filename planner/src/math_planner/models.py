from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TurnResult(BaseModel):
    """Minimal Codex transport result used by the Contract-first Planner backend."""

    thread_id: str
    turn_id: str
    status: str
    text: str
    item_type: str = "agentMessage"
    usage: dict[str, int] = Field(default_factory=dict)
    error: dict[str, Any] | str | None = None
