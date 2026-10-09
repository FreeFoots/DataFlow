from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SqlExecution:
    sql: str
    success: bool
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    execution_ms: float = 0.0
    truncated: bool = False
    limited: bool = False
