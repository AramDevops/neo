"""The model/tool loop that drives one agent turn.

Each iteration: build the prompt, call the model, parse the JSON contract,
store the plan, let the runtime controller inject required calls, execute the
tool batch, and decide whether to loop again. All progress is written into the
shared TurnState; conclusions (verdict/blocked/stopped) are NOT drawn here -
that is the runner's finalize phase."""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from ...config import Settings
from ..plan_engine import PlanEngine
from ..runtime_controller import RuntimeController
from ..tools import Toolbox
from .model_io import ModelClient
from .prompting import compact_args
from .state import TurnState

PARSE_RETRY_MAX = 2
# How many tool calls execute per loop; the rest are reported back so the
# model re-issues them instead of silently losing them.
MAX_TOOL_CALLS_PER_LOOP = 6


class TurnLoop:
    def __init__(
        self,
        toolbox: Toolbox,
        plan_engine: PlanEngine,
        runtime_controller: RuntimeController,
        model: ModelClient,
        build_prompt: Callable[[int, str, List[Dict[str, Any]]], str],
        event: Callable[[int, str, Dict[str, Any]], None],
        check_stop: Callable[[int], None],
    ) -> None:
        self.toolbox = toolbox
        self.plan_engine = plan_engine
        self.runtime_controller = runtime_controller
        self.model = model
        # Injected as a callable (not a PromptBuilder) so it resolves through
        # the runner at call time: tests patch prompt methods on AgentRunner
        # and must keep steering live loops.
        self.build_prompt = build_prompt
        self.event = event
        self.check_stop = check_stop

    def run(
        self,
        run_id: int,
        agent_id: int,
        user_message: str,
        provider: Any,
        state: TurnState,
    ) -> None:
        """Drive the model/tool loop, mutating `state`. Raises RunStopRequested
        at a checkpoint if the user asked to stop."""
        for loop_index in range(Settings.max_agent_loops):
            self.check_stop(run_id)
            state.loop_count = loop_index + 1
            not_last_loop = loop_index < Settings.max_agent_loops - 1
            prompt = self.build_prompt(agent_id, user_message, state.observations)
            result = self.model.generate_with_retries(provider, prompt, run_id, loop_index + 1, images=state.pending_images)
            state.pending_images = []
            state.token_estimate += result.token_estimate
            self.event(run_id, "model_response", {
                "loop": loop_index + 1,
                "latency_ms": result.latency_ms,
                "text": result.text,
            })

            parsed = self.model.parse_model_json(result.text)
            if parsed.get("_parse_failed") and state.parse_retries < PARSE_RETRY_MAX and not_last_loop:
                # The model answered in prose instead of the JSON contract.
                # Re-prompt once (bounded) so it acts on its stated plan
                # instead of narrating it and stopping.
                state.parse_retries += 1
                self._policy_retry(run_id, state, (
                    "Your previous response was not valid JSON and could not be executed. "
                    "Respond with ONLY the JSON object {\"plan\":[...],\"tool_calls\":[...],\"final\":\"...\",\"needs_more\":false} "
                    "and put the tool calls you described into tool_calls so they actually run."
                ), {"parse_retry": True}, "parse_retry", {"attempt": state.parse_retries, "max": PARSE_RETRY_MAX})
                continue

            state.latest_plan = parsed.get("plan", []) or state.latest_plan
            stored_plan = self.plan_engine.store_plan(run_id, state.latest_plan, user_message)
            # The controller drives off the STORED (normalized/expanded)
            # plan: it carries the canonical "Start the app / Open the
            # browser" steps even when the user message and the model's
            # own phrasing miss every run keyword (run 65).
            tool_calls = self.runtime_controller.required_tool_calls(
                user_message, state.observations, parsed.get("tool_calls") or [], stored_plan or state.latest_plan
            )
            state.final_text = parsed.get("final") or state.final_text or result.text
            if not tool_calls:
                if parsed.get("needs_more") and not_last_loop:
                    # The model's own continuation signal: it says the task is
                    # unfinished but issued no calls. Honor the field instead
                    # of ignoring it - ask for the calls.
                    self._policy_retry(run_id, state, (
                        "You set needs_more=true but issued no tool_calls. Either issue the tool calls "
                        "for the next step now, or set needs_more=false and put your complete answer in final."
                    ), {"needs_more_retry": True}, "policy_retry", {"reason": "needs_more without tool_calls"})
                    continue
                retry_reason = self.runtime_controller.continue_without_tools_reason(
                    user_message, state.latest_plan, state.final_text, state.observations
                )
                if retry_reason and not_last_loop:
                    self._policy_retry(run_id, state, retry_reason, {"continue_required": True}, "policy_retry", {"reason": retry_reason})
                    continue
                state.ended_after_tool_call = False
                break

            batch_failed = self._execute_tool_batch(run_id, tool_calls, state)
            state.ended_after_tool_call = True
            if batch_failed:
                continue

    def _execute_tool_batch(self, run_id: int, tool_calls: List[Any], state: TurnState) -> bool:
        """Run up to MAX_TOOL_CALLS_PER_LOOP calls, recording each observation.
        Returns True if any call failed. Dropped calls are reported back."""
        batch_failed = False
        for call in tool_calls[:MAX_TOOL_CALLS_PER_LOOP]:
            self.check_stop(run_id)
            tool_name = str(call.get("tool", ""))
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            tool_result = self.toolbox.execute(tool_name, args)
            if not tool_result.ok:
                batch_failed = True
            state.tool_count += 1
            payload = {
                "tool": tool_result.tool or tool_name,
                "requested_tool": tool_name,
                # Full args already reached the tool; the history only needs a
                # preview (write_file args carry entire files and used to be
                # re-billed verbatim on every loop).
                "args": compact_args(args),
                "ok": tool_result.ok,
                "output": tool_result.output,
                "meta": tool_result.meta,
            }
            state.observations.append(payload)
            self.event(run_id, "tool_result", payload)
            self.plan_engine.advance_for_observation(run_id, payload)
            screenshot_path = (tool_result.meta or {}).get("screenshot_path")
            if screenshot_path:
                state.pending_images.append(str(screenshot_path))
        dropped_calls = tool_calls[MAX_TOOL_CALLS_PER_LOOP:]
        if dropped_calls:
            # Never silently discard requested work: tell the model exactly
            # which calls did not run so it re-issues them.
            dropped_names = [str(item.get("tool", "?")) for item in dropped_calls if isinstance(item, dict)]
            state.observations.append({
                "tool": "neo_policy",
                "args": {},
                "ok": True,
                "output": (
                    f"Only the first {MAX_TOOL_CALLS_PER_LOOP} tool calls were executed this turn. "
                    f"These {len(dropped_names)} calls did NOT run and must be re-issued next turn: {', '.join(dropped_names)}."
                ),
                "meta": {"dropped_tool_calls": dropped_names},
            })
        return batch_failed

    def _policy_retry(
        self,
        run_id: int,
        state: TurnState,
        output: str,
        meta: Dict[str, Any],
        event_type: str,
        event_payload: Dict[str, Any],
    ) -> None:
        """Append a neo_policy nudge, emit its event, and reset the turn's
        pending answer so the loop takes another step instead of finishing on
        stale text. The one shared shape for every re-prompt."""
        state.observations.append({"tool": "neo_policy", "args": {}, "ok": True, "output": output, "meta": meta})
        self.event(run_id, event_type, event_payload)
        state.final_text = ""
        state.ended_after_tool_call = False
