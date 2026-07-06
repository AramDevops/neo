from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class TurnState:
    """The single source of truth for a run's mutable progress.

    Every phase of run_turn (loop, verdict, stop, error) reads and writes this
    one object instead of threading a dozen local variables by hand."""

    observations: List[Dict[str, Any]] = field(default_factory=list)
    latest_plan: List[Any] = field(default_factory=list)
    final_text: str = ""
    tool_count: int = 0
    token_estimate: int = 0
    loop_count: int = 0
    ended_after_tool_call: bool = False
    pending_images: List[str] = field(default_factory=list)
    parse_retries: int = 0
