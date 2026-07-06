"""What kind of request is this?

IntentRegistry caches the model's structured intent classification per
message. IntentDetectors answers the gate questions ("is this a status
question?", "does this need a browser?"), trusting a classified intent first
(language-independent); the keyword branches are the FALLBACK for when
classification is unavailable, never the primary mechanism.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from .text import bounded_edit_distance, has_near_token


class IntentRegistry:
    """Classified intents keyed by normalized message."""

    def __init__(self) -> None:
        self._intents: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _key(user_message: str) -> str:
        return re.sub(r"\s+", " ", (user_message or "").strip().lower())[:400]

    def note(self, user_message: str, intent: Dict[str, Any] | None) -> None:
        key = self._key(user_message)
        if not key:
            return
        if intent is None:
            self._intents.pop(key, None)
            return
        if len(self._intents) > 64:
            self._intents.clear()
        self._intents[key] = dict(intent)

    def get(self, user_message: str) -> Dict[str, Any] | None:
        return self._intents.get(self._key(user_message))


class IntentDetectors:
    """Every gate question about the user's message, in one place. Each
    detector consults the classified intent first and falls back to its
    conservative keyword heuristic."""

    def __init__(self, registry: IntentRegistry) -> None:
        self._registry = registry

    # Vocabulary the fuzzy canonicalizer may snap words to. Deliberately tiny:
    # this is typo tolerance for screen-view intent, not a general keyword
    # expansion layer.
    _SCREEN_VOCABULARY = ("screen", "desktop", "monitor", "display", "screenshot", "capture")

    def screen_view_required(self, user_message: str) -> bool:
        """True when the user asks the agent to look at their screen
        ("do you see my screen?", "take a screenshot"). Seeing requires a
        screen_capture observation, so required_tool_calls injects one instead
        of trusting the model to reach for the tool. flash-tier models answer
        "I am a text-based agent" from priors otherwise.
        """
        raw_words = [word.replace("'", "") for word in re.findall(r"[a-z']+", (user_message or "").lower())]
        words = [self._canonical_screen_word(word) for word in raw_words if word]
        if not words:
            return False
        text = " ".join(words)
        if re.search(r"\b(?:screenshot|screencap|screengrab)\b", text):
            return True
        if re.search(r"\b(?:screen shot|screen capture|print screen|capture (?:my|the|this) screen)\b", text):
            return True
        surface = re.search(r"\b(?:my|the|this) (?:screen|desktop|monitor|display)\b", text) or re.search(r"\bon (?:my )?screen\b", text)
        if not surface:
            return False
        sight_words = {
            "see", "sees", "seeing", "seen", "look", "looks", "looking", "watch", "watching",
            "view", "viewing", "describe", "read", "reading", "observe", "check", "checking",
            "what", "whats",
        }
        return bool(set(words) & sight_words)

    def _canonical_screen_word(self, word: str) -> str:
        """Snap near-miss spellings onto the screen vocabulary ("screemn" ->
        "screen") with the shared bounded edit distance. Words under 5 chars
        pass through untouched; fuzzing short words is how false intents
        happen."""
        if len(word) < 5:
            return word
        for target in self._SCREEN_VOCABULARY:
            if word == target:
                return target
            tolerance = 1 if len(target) <= 7 else 2
            if abs(len(word) - len(target)) <= tolerance and bounded_edit_distance(word, target, tolerance) <= tolerance:
                return target
        return word

    def workspace_inventory_required(self, user_message: str) -> bool:
        """"What apps do we have?" is an inventory question: the answer must
        cover what EXISTS in the workspace, not only what is running (run 94
        answered "nothing is running" for a workspace full of projects).
        Conservative: runtime-state questions ("is the app running", "which
        port") and action requests ("run/open the app") keep their narrow,
        correct handling."""
        words = [word.replace("'", "") for word in re.findall(r"[a-z']+", (user_message or "").lower())]
        if not words:
            return False
        word_set = set(words)
        subjects = {"app", "apps", "application", "applications", "project", "projects", "site", "sites", "website", "websites"}
        if not (word_set & subjects):
            return False
        if word_set & {"running", "started", "active", "listening", "port", "ports", "open", "start", "run", "launch", "browser"}:
            return False
        asking = bool(word_set & {"what", "whats", "which", "list", "show", "inventory"}) or "we have" in " ".join(words)
        if not asking:
            return False
        return self.is_status_question(user_message) or words[0] in {"list", "show"}

    def coordination_status_required(self, user_message: str) -> bool:
        lowered = user_message.lower()
        coordination_terms = [
            "other ai", "other agent", "other agents", "another ai", "another agent",
            "all agents", "agents doing", "ai doing", "who is doing", "what are they doing",
            "shared context", "team status",
        ]
        return any(term in lowered for term in coordination_terms)

    def is_status_question(self, user_message: str) -> bool:
        """True for information questions ("is there any app running") that must
        be answered with observation, never treated as a work order.

        Conservative on purpose: polite requests ("can you run the app") and
        imperatives stay commands.
        """
        intent = self._registry.get(user_message)
        if intent:
            return intent.get("kind") == "status_question"
        words = re.findall(r"[a-z']+", (user_message or "").lower())
        if not words:
            return False
        status_starts = {
            "is", "are", "was", "were", "am", "do", "does", "did", "has", "have", "had",
            "any", "what", "whats", "which", "who", "when", "where", "why", "how", "status",
        }
        if words[0] not in status_starts:
            return False
        action_verbs = {
            "run", "start", "launch", "open", "build", "create", "make", "install",
            "fix", "move", "organize", "organise", "serve", "deploy", "restart", "stop",
        }
        directed_request = len(words) >= 2 and words[0] in {"do", "does", "did"} and words[1] == "you" and bool(set(words[2:]) & action_verbs)
        return not directed_request

    def goal_requires_work(self, user_message: str) -> bool:
        intent = self._registry.get(user_message)
        if intent:
            kind = intent.get("kind")
            if kind == "status_question":
                return False
            if kind == "general":
                return bool(intent.get("wants_changes") or intent.get("needs_running_app"))
            return True
        if self.is_status_question(user_message):
            return False
        lowered = user_message.lower()
        return any(term in lowered for term in [
            "build", "create", "run", "open", "launch", "fix", "write", "edit", "install", "serve",
            "test", "verify", "organize", "organise", "clean", "improve", "better", "refactor",
            "debug", "repair", "update", "modify", "implement", "audit", "scan", "review", "diagnose",
            "assess", "security", "virus", "virirses", "malware", "trojan", "arrange", "arage", "messy", "tidy", "structure",
            "upgrade", "add", "extend", "integrate", "rewrite", "redesign",
        ])

    # A request that asks the model to EXPLAIN or to REFRAIN from acting (a safety
    # refusal, "how would you", "do not delete") legitimately makes no changes.
    # A fallible intent classifier sometimes tags these wants_changes=true; hard-
    # blocking them would punish a correct refusal (a small model asked to "explain
    # the safe alternative and do not call destructive tools" did the right thing
    # and got blocked for it). A build verb in the message overrides this, so
    # "add X without touching Y" still requires the change.
    _EXPLAIN_REFUSE_SIGNALS = (
        "explain", "describe", "do not", "don't", "instead of", "without ",
        "what is", "what are", "how would", "how do", "why ", "safe alternative",
    )
    _BUILD_VERB_RE = re.compile(
        r"\b(?:add|implement|build|creat|writ|make|generat|scaffold|upgrade|"
        r"extend|integrate|rewrite|redesign|refactor|fix|install|set ?up)\w*\b"
    )

    def explanation_or_refusal(self, user_message: str) -> bool:
        lowered = (user_message or "").lower()
        if self._BUILD_VERB_RE.search(lowered):
            return False
        return any(sig in lowered for sig in self._EXPLAIN_REFUSE_SIGNALS)

    def modification_required(self, user_message: str) -> bool:
        """True when the user asked for something to be BUILT INTO the work
        ("upgrade the app, add a login"): a run that then writes nothing has,
        by definition, not done it. Run 120 claimed completion for an add-a-
        login request whose only actions were reads and a server restart."""
        # A pure explanation or refusal makes no changes by design; never hard-
        # block it, even when a fallible classifier tagged it wants_changes.
        if self.explanation_or_refusal(user_message):
            return False
        intent = self._registry.get(user_message)
        if intent:
            return bool(intent.get("wants_changes"))
        if self.is_status_question(user_message) or self.is_web_action(user_message) or self.is_desktop_gui_task(user_message):
            return False
        lowered = (user_message or "").lower()
        return bool(re.search(r"\b(?:add|upgrade|implement|extend|integrate|rewrite|redesign)\b", lowered))

    def organization_required(self, user_message: str) -> bool:
        intent = self._registry.get(user_message)
        if intent:
            return intent.get("kind") == "organization"
        lowered = user_message.lower()
        organization_terms = [
            "organize", "organise", "orgnsi", "clean up", "tidy", "structure", "group",
            "put", "move", "arrange", "arage", "messy", "workspace better",
        ]
        object_terms = [
            "workspace", "folder", "directory", "files", "project", "app", "application",
            "backend", "frontend", "calendar", "cladnare",
        ]
        return any(term in lowered for term in organization_terms) and any(term in lowered for term in object_terms)

    def is_web_action(self, user_message: str) -> bool:
        """True for web-search / open-a-website requests ("search Google for X",
        "open youtube"). These are handled by research_web / open_browser and
        must NOT trigger the local app-run pipeline (project_probe -> install ->
        start); with leftover app projects in the workspace, that pipeline
        hijacked plain web searches and opened an unrelated local app."""
        intent = self._registry.get(user_message)
        if intent:
            return intent.get("kind") == "web_action"
        lowered = user_message.lower()
        if any(term in lowered for term in ["search the web", "search for", "web search", "search google", "google for", "look up", "search on"]):
            return True
        if re.search(r"\bsearch\b", lowered) and "app" not in lowered:
            return True
        if re.search(r"\bgoogle\b", lowered) and "app" not in lowered:
            return True
        if "open" in lowered:
            web_targets = ["google", "youtube", "http://", "https://", "www.", ".com", ".org", ".net",
                           "website", "web page", "webpage", "chrome", "browser tab", "new tab", "a tab"]
            if any(term in lowered for term in web_targets):
                return True
        return False

    # Desktop applications operated by pixel control, not built or served. A
    # request naming one of these is computer-use, not an app-run: it has no
    # localhost URL and no browser step.
    _DESKTOP_APPS = (
        "calculator", "calc", "notepad", "wordpad", "mspaint", "paint",
        "file explorer", "explorer", "control panel", "task manager", "taskmgr",
        "snipping tool", "registry editor", "regedit", "command prompt",
    )
    _GUI_ACTIONS = (
        "click the", "click on", "double click", "double-click", "right click",
        "right-click", "the button", "the buttons", "press the button",
        "type into", "the mouse", "the cursor", "desktop icon", "start menu",
        "system tray", "taskbar", "on my desktop",
    )

    def is_desktop_gui_task(self, user_message: str) -> bool:
        """True for operating a native desktop app or GUI by mouse/keyboard
        ("open calculator and click the buttons"). Such a task must NOT enter
        the app-build/run pipeline or the browser-verification gate: there is
        no project to probe, no dependency to install, no port, and no URL.

        The recurring failure (run 104): "open calculator ... clicking the
        buttons" was routed as an app-run, dragged in leftover Node projects
        via project_probe/node_install/start_process, then blocked forever on a
        browser URL a calculator can never have.
        """
        intent = self._registry.get(user_message)
        if intent:
            return intent.get("kind") == "desktop_gui"
        lowered = (user_message or "").lower()
        if not lowered.strip():
            return False
        if self.is_web_action(user_message):
            return False  # web/search/open-website wins; handled by the web tools
        # Building or creating a project (even "a calculator app") is NOT this:
        # those words route to the app-build pipeline on purpose.
        if re.search(r"\b(app|application|website|web ?app|web ?page|webpage|site|browser|localhost|server)\b", lowered):
            return False
        if any(name in lowered for name in self._DESKTOP_APPS):
            return True
        return any(term in lowered for term in self._GUI_ACTIONS)

    # Corroborating evidence that a task really is about a served app. The
    # classifier misroutes ~1 in 5 tool/compute tasks to an app kind ("compute
    # the sum of squares" -> app_run), and trusting that bare label made the
    # harness demand a browser for a math problem. An app kind is honored only
    # with one of these signals, mirroring the keyword fallback's app_terms.
    _APP_SIGNAL_TERMS = (
        "app", "application", "flask", "react", "node", "frontend", "backend",
        "server", "serve", "localhost", "website", "web site", "webpage", "web page",
        "site", "dashboard", "endpoint", "http://", "https://", "browser",
        "port ", "deploy", "calendar",
    )

    def app_signal(self, context: str) -> bool:
        low = (context or "").lower()
        return any(term in low for term in self._APP_SIGNAL_TERMS)

    def browser_required(self, user_message: str, plan: List[Any], final_text: str) -> bool:
        context = " ".join([user_message, final_text or "", " ".join(map(str, plan))]).lower()
        intent = self._registry.get(user_message)
        if intent:
            if intent.get("needs_running_app"):
                return True
            # needs_running_app is false: trust it for app_modify. Modifying or
            # fixing existing code (a library bug fix) serves nothing - every
            # SWE-bench code fix was wrongly browser-blocked because the issue
            # text mentions http/server as concepts, not as an app to run.
            # app_run/app_build inherently mean a served app, so corroborate
            # those with an app signal before demanding a browser.
            if intent.get("kind") in {"app_run", "app_build"}:
                return self.app_signal(context)
            return False
        if self.is_status_question(user_message) or self.is_web_action(user_message):
            return False
        if self.is_desktop_gui_task(user_message):
            return False
        user = user_message.lower()
        # upgrade/fix/add count as run intents: changing an app is only done
        # when the changed app demonstrably serves (run 120 completed an
        # add-a-login request without the login ever existing).
        run_terms = {"build", "run", "open", "launch", "preview", "serve", "start", "upgrade", "fix", "add"}
        app_terms = {"app", "application", "flask", "react", "node", "frontend", "backend", "server", "localhost", "website", "site", "calendar"}
        # "The app is broken / doesn't work / won't run" is an app-run task even
        # with no run verb: the run must not complete until the app actually
        # serves. Run 116/117: a broken-app follow-up got hijacked into a
        # security audit and falsely completed with the app still down.
        broken_terms = {"broken", "borken", "doesn't work", "doesnt work", "not working",
                        "isn't working", "isnt working", "wont run", "won't run", "still broken", "not run"}
        if "browser" in context:
            return True
        if any(term in user for term in broken_terms) and any(term in context for term in app_terms):
            return True
        return any(term in user for term in run_terms) and any(term in context for term in app_terms)

    def security_audit_required(self, user_message: str) -> bool:
        intent = self._registry.get(user_message)
        if intent:
            return intent.get("kind") == "security_audit"
        lowered = user_message.lower()
        tokens = re.findall(r"[a-z0-9]+", lowered)
        audit_terms = ["audit", "audsit", "check", "scan", "review", "diagnose", "assess"]
        security_terms = [
            "security", "secure", "secuity", "securty", "secutiy", "securoty",
            "vulnerability", "vulnerabilities", "secret", "secrets", "dependency", "dependencies",
            "virus", "viruses", "virirses", "malware", "trojan", "ransomware", "defender", "antivirus",
        ]
        system_terms = ["current", "xurrent", "os", "host", "system", "windows", "linux", "machine", "computer", "environment", "workspace"]
        audit_hit = any(term in lowered for term in audit_terms) or has_near_token(tokens, ["audit"], max_distance=2)
        security_hit = any(term in lowered for term in security_terms) or has_near_token(tokens, ["security", "secure"], max_distance=2)
        system_hit = any(term in lowered for term in system_terms)
        # A real security NOUN is mandatory. The old "audit word + system word"
        # path (no security noun) fired on pure noise: run 117 ran a security
        # audit on a broken clock app because the pasted error said "MIME
        # checking" (-> audit) and a WinError said "the target machine actively
        # refused" (-> system). "check"/"scan"/"machine" appearing in quoted
        # tool errors must never mean the user asked for a security audit.
        if security_hit and (audit_hit or system_hit):
            return True
        return any(term in lowered for term in ["os security", "host security", "system security", "security audit", "secrets scan", "dependency audit"])

    def vague_work_required(self, user_message: str) -> bool:
        lowered = user_message.lower()
        vague_terms = [
            "organize", "organise", "orgnsi", "arrange", "arage", "messy", "tidy",
            "clean up", "clean", "fix", "build", "improve", "better",
            "usable", "logical", "industry-grade", "refactor", "debug", "repair", "make it",
            "audit", "security", "secure", "vulnerability", "vulnerabilities",
            "virus", "viruses", "virirses", "malware", "antivirus",
        ]
        return any(term in lowered for term in vague_terms)

    def app_work_required(self, user_message: str) -> bool:
        lowered = user_message.lower()
        work_terms = {"build", "create", "make", "run", "open", "launch", "preview", "serve", "start"}
        app_terms = {"app", "application", "flask", "react", "node", "frontend", "backend", "server", "localhost", "website", "site", "calendar"}
        return any(term in lowered for term in work_terms) and any(term in lowered for term in app_terms)
