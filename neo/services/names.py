from __future__ import annotations

import random


FIRST_NAMES = [
    "Mira", "Noor", "Vega", "Ari", "Sol", "Iris", "Niko", "Lina",
    "Kian", "Rami", "Tala", "Milo", "Sana", "Eli", "Nova", "Yara",
]

ROLES = [
    "planner", "builder", "critic", "operator", "tester", "analyst",
    "scribe", "navigator", "debugger", "auditor",
]


def generate_agent_name(existing_names: set[str] | None = None) -> tuple[str, str]:
    existing_names = existing_names or set()
    for _ in range(80):
        name = random.choice(FIRST_NAMES)
        role = random.choice(ROLES)
        candidate = f"{name}-{role}"
        if candidate not in existing_names:
            return candidate, role
    return f"agent-{random.randint(1000, 9999)}", "agent"
