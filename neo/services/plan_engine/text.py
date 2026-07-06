"""Bounded fuzzy-matching helpers shared by the intent detectors.

Typo tolerance is deliberately conservative: short tokens are never fuzzed
(fuzzing short words is how false intents happen), and the edit distance
gives up as soon as a whole row exceeds the cutoff.
"""

from __future__ import annotations

from typing import List


def bounded_edit_distance(left: str, right: str, cutoff: int) -> int:
    """Levenshtein distance that returns cutoff + 1 as soon as the distance
    provably exceeds the cutoff."""
    previous = list(range(len(right) + 1))
    for row_index, left_char in enumerate(left, 1):
        current = [row_index]
        row_best = current[0]
        for col_index, right_char in enumerate(right, 1):
            insert_cost = current[col_index - 1] + 1
            delete_cost = previous[col_index] + 1
            replace_cost = previous[col_index - 1] + (left_char != right_char)
            value = min(insert_cost, delete_cost, replace_cost)
            current.append(value)
            row_best = min(row_best, value)
        if row_best > cutoff:
            return cutoff + 1
        previous = current
    return previous[-1]


def has_near_token(tokens: List[str], targets: List[str], max_distance: int = 1) -> bool:
    """True when any token sits within max_distance edits of any target.
    Tokens under 4 chars never match."""
    for token in tokens:
        if len(token) < 4:
            continue
        for target in targets:
            if abs(len(token) - len(target)) > max_distance:
                continue
            if bounded_edit_distance(token, target, max_distance) <= max_distance:
                return True
    return False
