from __future__ import annotations

import threading


class RunStopRequested(Exception):
    """Raised at a checkpoint inside the agent loop when the user asked to stop."""


class StopSignal:
    """Pending user stop requests, keyed by run id.

    Run ids are unique and never reused, so a request can only ever affect the
    run it targeted. The stop is cooperative: the loop, tool batch, and
    provider-retry paths call check() at their checkpoints; a model call or
    tool call already in flight finishes first."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Exposed as AgentRunner._stop_requests (same set object) so tests can
        # inspect and seed it directly.
        self.requests: set[int] = set()

    def request(self, run_id: int) -> None:
        with self._lock:
            self.requests.add(int(run_id))

    def consume(self, run_id: int) -> bool:
        """Clear a pending request for this run, reporting whether one existed."""
        with self._lock:
            if int(run_id) in self.requests:
                self.requests.discard(int(run_id))
                return True
            return False

    def check(self, run_id: int) -> None:
        """Raise RunStopRequested when a stop is pending for this run."""
        with self._lock:
            pending = int(run_id) in self.requests
        if pending:
            raise RunStopRequested()
