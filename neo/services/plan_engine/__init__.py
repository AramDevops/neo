"""Deterministic plan gating for agent runs.

Split by concern; PlanEngine (engine.py) is the facade the rest of the app
imports, so `from neo.services.plan_engine import PlanEngine` keeps working
exactly as it did when this was a single module.
"""

from .engine import EventSink, PlanEngine

__all__ = ["EventSink", "PlanEngine"]
