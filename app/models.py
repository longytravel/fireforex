"""Pydantic request/response shapes for the Fire Forex web API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    ea: dict[str, Any] = Field(..., description="EA JSON (ea_to_dict output)")
    n_trials: int = Field(2000, ge=10, le=50_000_000)
    seed: int = Field(42, ge=0)
    layer_name: str | None = Field(None, description="History row label")
    artifact_mode: str = Field("auto", description="auto, rich, or lean")
    chunk_size: int | None = Field(None, ge=1)


class JobProgress(BaseModel):
    status: str  # "running" | "done" | "error"
    progress: float  # 0.0..1.0
    message: str = ""
    started_at: float  # unix epoch seconds
    finished_at: float | None = None
    error: str | None = None
    result: dict[str, Any] | None = None


class DefaultsRequest(BaseModel):
    pair: str
    main_tf: str
    sub_tf: str | None = None
    level: int = Field(6, ge=1, le=10)
    name: str | None = None
