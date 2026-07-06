from __future__ import annotations

import re
from typing import Any, Mapping


def agent_label(agent_id: Any, agent: Mapping[str, Any] | None = None) -> str:
    name = str((agent or {}).get("name") or "").strip()
    if name:
        return name
    clean_id = str(agent_id or "").strip()
    return f"agent-{clean_id}" if clean_id else "agent"


def replace_agent_refs(text: Any, agent_names: Mapping[int, str]) -> str:
    value = str(text or "")

    def replace_title(match: re.Match[str]) -> str:
        agent_id = int(match.group(1))
        return agent_names.get(agent_id, match.group(0))

    def replace_tag(match: re.Match[str]) -> str:
        agent_id = int(match.group(1))
        if agent_id not in agent_names:
            return match.group(0)
        return f"agent:{agent_names[agent_id]}"

    value = re.sub(r"\bAgent\s+(\d+)\b", replace_title, value)
    return re.sub(r"\bagent:(\d+)\b", replace_tag, value)
