from __future__ import annotations

from pydantic import BaseModel, Field


class TurnResult(BaseModel):
    """Minimal Codex transport result used by the Dynamic Prover backend."""

    thread_id: str
    status: str
    text: str
    usage: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
    stream_path: str | None = None
