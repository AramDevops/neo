"""Internal building blocks of the agent runner.

`neo.services.agent_runner.AgentRunner` is the composition root and the only
public entry point; these modules each own one concern of a run:

- stop:      cooperative user-stop signal shared across threads
- state:     the mutable TurnState threaded through one turn
- journal:   every DB/artifact write in a run's lifecycle
- prompting: prompt assembly and context budgeting
- model_io:  provider calls, retry/backoff, JSON contract parsing
- loop:      the model/tool loop driver
- recovery:  auto-recovery policy for blocked runs
"""

from .journal import RunJournal, utc_stamp
from .loop import MAX_TOOL_CALLS_PER_LOOP, PARSE_RETRY_MAX, TurnLoop
from .model_io import PROVIDER_RETRY_MAX, ModelClient
from .prompting import OBSERVATION_CHAR_BUDGET, PromptBuilder
from .recovery import RecoveryPolicy
from .state import TurnState
from .stop import RunStopRequested, StopSignal

__all__ = [
    "MAX_TOOL_CALLS_PER_LOOP",
    "OBSERVATION_CHAR_BUDGET",
    "PARSE_RETRY_MAX",
    "PROVIDER_RETRY_MAX",
    "ModelClient",
    "PromptBuilder",
    "RecoveryPolicy",
    "RunJournal",
    "RunStopRequested",
    "StopSignal",
    "TurnLoop",
    "TurnState",
    "utc_stamp",
]
