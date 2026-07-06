from __future__ import annotations

import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List

from .plan_engine import PlanEngine
from .tools import Toolbox


EventSink = Callable[[int, str, Dict[str, Any]], None]
PlanAdvance = Callable[[int, Dict[str, Any]], None]


class BrowserVerifier:
    """Verifies app-run work with concrete HTTP and browser evidence."""

    def __init__(
        self,
        toolbox: Toolbox,
        plan_engine: PlanEngine,
        event_sink: EventSink,
        advance_plan: PlanAdvance,
        runtime_controller: Any | None = None,
    ) -> None:
        self.toolbox = toolbox
        self.plan_engine = plan_engine
        self.event_sink = event_sink
        self.advance_plan = advance_plan
        # Optional: enables the verdict-time deterministic tail (auto-start of
        # the known target project when the model never got there).
        self.runtime_controller = runtime_controller

    def ensure(
        self,
        run_id: int,
        user_message: str,
        plan: List[Any],
        final_text: str,
        observations: List[Dict[str, Any]],
    ) -> tuple[str, int, str]:
        if not self.plan_engine.browser_required(user_message, plan, final_text):
            return final_text, 0, ""
        verified_url = self.verified_browser_url(observations)
        if verified_url:
            return final_text, 0, ""

        extra_count = 0
        url = self.best_app_url(user_message, final_text, observations)
        if not url:
            # Deterministic tail: the run needs a serving app but never
            # (re)started one, usually a weak model exhausting its loops on
            # reads after writing real code (run 123: login implemented, app
            # never restarted, run blocked on "unfinished steps"). The harness
            # knows the target project and its start script; walking the
            # start -> verify -> browser tail is infrastructure work, not
            # model work.
            started, start_calls = self._start_target_project(run_id, user_message, observations)
            extra_count += start_calls
            if started is not None and started.get("ok"):
                url = self.best_app_url(user_message, final_text, observations)
        if not url:
            reason = "Browser verification blocked: no verified app URL could be inferred from a started process, HTTP check, browser attempt, or the user message."
            return self._append_note(final_text, reason), extra_count, reason
        if not self.has_successful_http_for_url(observations, url):
            http_payload: Dict[str, Any] | None = None
            for attempt in range(1, 5):
                if attempt > 1:
                    time.sleep(min(0.75 * attempt, 2.5))
                    self.event_sink(run_id, "recovery_attempt", {
                        "kind": "http_retry",
                        "attempt": attempt,
                        "url": url,
                    })
                    self.check_recent_processes(run_id, observations)

                http_result = self.toolbox.execute("http_get", {"url": url})
                extra_count += 1
                http_payload = {
                    "tool": "http_get",
                    "args": {"url": url, "attempt": attempt},
                    "ok": http_result.ok,
                    "output": http_result.output,
                    "meta": http_result.meta,
                }
                observations.append(http_payload)
                self.event_sink(run_id, "tool_result", http_payload)
                self.advance_plan(run_id, http_payload)
                if http_result.ok:
                    break
            if not http_payload or not http_payload.get("ok"):
                reason = f"Browser verification blocked: {url} did not respond after retries."
                return self._append_note(final_text, reason), extra_count, reason

        # A 200 on the root URL is not a working app: a page whose css/js come
        # back 404 (or as text/html fallbacks) renders blank while every naive
        # check passes. Verify the assets the page references before opening
        # the browser or claiming success.
        asset_reason, asset_calls = self.verify_page_assets(run_id, url, observations)
        extra_count += asset_calls
        if asset_reason:
            return self._append_note(final_text, asset_reason), extra_count, asset_reason

        if self.has_successful_browser_for_url(observations, url):
            return self._append_note(final_text, f"Opened browser: {url}"), extra_count, ""

        browser_payload: Dict[str, Any] | None = None
        for attempt in range(1, 3):
            browser_result = self.toolbox.execute("open_browser", {"url": url})
            extra_count += 1
            browser_payload = {
                "tool": "open_browser",
                "args": {"url": url, "attempt": attempt},
                "ok": browser_result.ok,
                "output": browser_result.output,
                "meta": browser_result.meta,
            }
            observations.append(browser_payload)
            self.event_sink(run_id, "tool_result", browser_payload)
            self.advance_plan(run_id, browser_payload)
            if browser_result.ok:
                return self._append_note(final_text, f"Opened browser: {url}"), extra_count, ""
            self.event_sink(run_id, "recovery_attempt", {
                "kind": "browser_retry",
                "attempt": attempt + 1,
                "url": url,
                "error": browser_result.output,
            })
            time.sleep(0.5)

        reason = f"Browser open blocked for {url}: {browser_payload.get('output') if browser_payload else 'unknown error'}"
        return self._append_note(final_text, reason), extra_count, reason

    _ASSET_REF_PATTERN = re.compile(r"(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", re.I)
    _ASSET_SUFFIXES = (".css", ".js", ".mjs")

    def page_asset_refs(self, html: str) -> List[str]:
        refs: List[str] = []
        for raw in self._ASSET_REF_PATTERN.findall(html or ""):
            clean = raw.strip()
            if not clean or clean.startswith(("#", "data:", "mailto:", "javascript:")):
                continue
            if clean.split("?")[0].lower().endswith(self._ASSET_SUFFIXES) and clean not in refs:
                refs.append(clean)
        return refs[:6]

    def verify_page_assets(self, run_id: int, url: str, observations: List[Dict[str, Any]]) -> tuple[str, int]:
        """Fetch the css/js the served page references and fail verification
        with concrete evidence when they are broken (404 or served as
        text/html by an SPA-style fallback). Only same-host assets are
        checked; external CDNs are the network's problem, not the app's."""
        page_html = ""
        for observation in reversed(observations):
            if observation.get("tool") != "http_get" or not observation.get("ok"):
                continue
            if self.normalize_url(self.observation_url(observation)) == self.normalize_url(url):
                page_html = str(observation.get("output") or "")
                break
        refs = self.page_asset_refs(page_html)
        if not refs:
            return "", 0

        page_host = urllib.parse.urlparse(url).netloc
        failures: List[str] = []
        calls = 0
        for ref in refs:
            asset_url = urllib.parse.urljoin(url.rstrip("/") + "/", ref)
            if urllib.parse.urlparse(asset_url).netloc != page_host:
                continue
            result = self.toolbox.execute("http_get", {"url": asset_url})
            calls += 1
            payload = {
                "tool": "http_get",
                "args": {"url": asset_url, "asset_of": url},
                "ok": result.ok,
                "output": result.output,
                "meta": result.meta,
            }
            observations.append(payload)
            self.event_sink(run_id, "tool_result", payload)
            content_type = str((result.meta or {}).get("content_type") or "").lower()
            if not result.ok:
                failures.append(f"{ref} -> {' '.join(str(result.output or 'request failed').split())[:100]}")
            elif "text/html" in content_type:
                failures.append(f"{ref} -> served as {content_type} (static files are not being served)")
        if failures:
            reason = (
                f"App verification blocked: {url} responds but the page's assets are broken: "
                + "; ".join(failures)
                + ". Fix the static file serving (wrong static dir or route) and verify again."
            )
            return reason, calls
        return "", calls

    def _start_target_project(
        self,
        run_id: int,
        user_message: str,
        observations: List[Dict[str, Any]],
    ) -> tuple[Dict[str, Any] | None, int]:
        """Start the run's known target project via the controller's start-call
        builder (same targeting, same dedupe: never re-starts a project that is
        already serving or whose start was already attempted this run). Returns
        (start observation | None, tool calls made)."""
        controller = self.runtime_controller
        if controller is None:
            return None, 0
        try:
            probes = controller._successful_project_probes(observations)
            probes = controller._target_probes(user_message, probes)
        except Exception:
            return None, 0
        for probe in probes[:2]:
            call = controller._runtime_start_call(probe, observations, [])
            if not call:
                continue
            result = self.toolbox.execute("start_process", call.get("args") or {})
            payload = {
                "tool": "start_process",
                "args": call.get("args") or {},
                "ok": result.ok,
                "output": result.output,
                "meta": result.meta,
            }
            observations.append(payload)
            self.event_sink(run_id, "tool_result", payload)
            self.advance_plan(run_id, payload)
            return payload, 1
        return None, 0

    def check_recent_processes(self, run_id: int, observations: List[Dict[str, Any]]) -> None:
        for observation in list(observations):
            if observation.get("tool") != "start_process" or not observation.get("ok"):
                continue
            meta = observation.get("meta") or {}
            pid = meta.get("pid")
            if not pid:
                continue
            result = self.toolbox.execute("process_status", {
                "pid": pid,
                "stdout_log": meta.get("stdout_log", ""),
                "stderr_log": meta.get("stderr_log", ""),
            })
            payload = {
                "tool": "process_status",
                "args": {
                    "pid": pid,
                    "stdout_log": meta.get("stdout_log", ""),
                    "stderr_log": meta.get("stderr_log", ""),
                },
                "ok": result.ok,
                "output": result.output,
                "meta": result.meta,
            }
            observations.append(payload)
            self.event_sink(run_id, "tool_result", payload)
            self.advance_plan(run_id, payload)

    def infer_app_url(self, texts: List[str]) -> str:
        haystack = "\n".join(texts)
        url_match = re.search(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[[^\]]+\]|[A-Za-z0-9.-]+):\d+(?:/[^\s\"'<>)]*)?", haystack, re.I)
        if url_match:
            return url_match.group(0).replace("0.0.0.0", "127.0.0.1").rstrip(".,;")
        port_match = re.search(r"\b(?:port|localhost:|127\.0\.0\.1:)\s*(\d{3,5})\b", haystack, re.I)
        if port_match:
            return f"http://127.0.0.1:{port_match.group(1)}"
        if "flask" in haystack.lower():
            return "http://127.0.0.1:5000"
        return ""

    def verified_browser_url(self, observations: List[Dict[str, Any]]) -> str:
        for url in self.successful_browser_urls(observations):
            if self.has_successful_http_for_url(observations, url):
                return url
        return ""

    # A URL ending in one of these is a static ASSET, not the app entrypoint.
    # Run-110 loop: the verifier scraped "http://127.0.0.1:3003/style.css" out
    # of the user's pasted console error and spent every retry "verifying" the
    # stylesheet instead of the app root, blocking forever.
    _ASSET_URL_SUFFIXES = (
        ".css", ".js", ".mjs", ".map", ".json", ".png", ".jpg", ".jpeg",
        ".gif", ".svg", ".ico", ".webp", ".woff", ".woff2", ".ttf", ".eot",
    )

    def app_root_of(self, url: str) -> str:
        """Collapse a URL to the app entrypoint. A stylesheet/script/image URL
        proves the app's ORIGIN (scheme://host:port), never the page to open,
        so drop an asset path down to its origin before treating it as the app
        URL. Non-asset paths (real routes) are kept."""
        clean = self.normalize_url(url)
        if not clean:
            return ""
        parsed = urllib.parse.urlparse(clean)
        path = (parsed.path or "").split("?")[0]
        if path.lower().endswith(self._ASSET_URL_SUFFIXES):
            return f"{parsed.scheme}://{parsed.netloc}"
        return clean

    def best_app_url(self, user_message: str, final_text: str, observations: List[Dict[str, Any]]) -> str:
        for url in self.successful_http_urls(observations):
            return self.app_root_of(url)
        route = self.first_app_route(observations)
        for url in self.started_process_urls(observations):
            if route:
                return f"{url}{route}"
            return url
        for url in self.successful_browser_urls(observations):
            return self.app_root_of(url)
        # URLs pulled from free text (the user's message or the model's final
        # answer) are the ones most likely to be a sub-asset; always collapse
        # them to the app root so a pasted /style.css error can't hijack the
        # verification target.
        user_url = self.first_url_from_text(user_message)
        if user_url:
            return self.app_root_of(user_url)
        if not observations:
            return self.app_root_of(self.infer_app_url([final_text]))
        return ""

    def successful_http_urls(self, observations: List[Dict[str, Any]]) -> List[str]:
        urls: List[str] = []
        for observation in observations:
            if not observation.get("ok") or observation.get("tool") not in {"http_get", "http_head"}:
                continue
            url = self.observation_url(observation)
            if url:
                urls.append(url)
        return self.unique_urls(urls)

    def successful_browser_urls(self, observations: List[Dict[str, Any]]) -> List[str]:
        urls: List[str] = []
        for observation in observations:
            if not observation.get("ok") or observation.get("tool") != "open_browser":
                continue
            url = self.observation_url(observation)
            if url:
                urls.append(url)
        return self.unique_urls(urls)

    def started_process_urls(self, observations: List[Dict[str, Any]]) -> List[str]:
        urls: List[str] = []
        for observation in observations:
            if not observation.get("ok") or observation.get("tool") != "start_process":
                continue
            meta = observation.get("meta") or {}
            for port in meta.get("ports") or []:
                try:
                    clean_port = int(port)
                except Exception:
                    continue
                if 1 <= clean_port <= 65535:
                    urls.append(f"http://127.0.0.1:{clean_port}")
            urls.extend(self.urls_from_text(str(observation.get("output") or "")))
            urls.extend(self.urls_from_text(str(meta.get("command") or "")))
        return self.unique_urls(urls)

    def first_app_route(self, observations: List[Dict[str, Any]]) -> str:
        texts: list[str] = []
        for observation in observations:
            if observation.get("tool") == "read_file" and observation.get("ok"):
                texts.append(str(observation.get("output") or ""))
            meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
            relative_path = self._clean_rel_path(meta.get("relative_path") or "")
            common = meta.get("common_files") if isinstance(meta.get("common_files"), dict) else {}
            if observation.get("tool") == "project_probe" and observation.get("ok") and common.get("app.py"):
                app_file = self._workspace_project_file(relative_path, "app.py")
                if app_file and app_file.exists():
                    try:
                        texts.append(app_file.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        pass
        for text in texts:
            routes = []
            for match in re.finditer(r"@app\.route\(\s*['\"]([^'\"]+)['\"]", text):
                route = match.group(1).strip()
                if not route or "<" in route or ">" in route:
                    continue
                routes.append(route if route.startswith("/") else f"/{route}")
            for route in routes:
                if route != "/":
                    return route
            if routes:
                return routes[0]
        return ""

    def has_successful_http_for_url(self, observations: List[Dict[str, Any]], url: str) -> bool:
        return self.normalize_url(url) in {self.normalize_url(item) for item in self.successful_http_urls(observations)}

    def has_successful_browser_for_url(self, observations: List[Dict[str, Any]], url: str) -> bool:
        return self.normalize_url(url) in {self.normalize_url(item) for item in self.successful_browser_urls(observations)}

    def observation_url(self, observation: Dict[str, Any]) -> str:
        args = observation.get("args") if isinstance(observation.get("args"), dict) else {}
        meta = observation.get("meta") if isinstance(observation.get("meta"), dict) else {}
        return str(meta.get("url") or args.get("url") or "").strip()

    def first_url_from_text(self, text: str) -> str:
        urls = self.urls_from_text(text)
        return urls[0] if urls else ""

    def urls_from_text(self, text: str) -> List[str]:
        found = re.findall(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[[^\]]+\]|[A-Za-z0-9.-]+):\d+(?:/[^\s\"'<>)]*)?", text, re.I)
        return self.unique_urls(url.replace("0.0.0.0", "127.0.0.1").rstrip(".,;") for url in found)

    def unique_urls(self, urls: Any) -> List[str]:
        unique: List[str] = []
        seen: set[str] = set()
        for url in urls:
            normalized = self.normalize_url(str(url or ""))
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)
        return unique

    def normalize_url(self, url: str) -> str:
        clean = str(url or "").strip().replace("0.0.0.0", "127.0.0.1").rstrip(".,;")
        if not clean:
            return ""
        # localhost and 127.0.0.1 are the same host; without canonicalizing
        # them the verifier treated a model-opened localhost URL as different
        # from its own 127.0.0.1 URL and opened the browser a second time.
        clean = clean.replace("://localhost:", "://127.0.0.1:").replace("://localhost/", "://127.0.0.1/")
        if clean.endswith("://localhost"):
            clean = clean[: -len("localhost")] + "127.0.0.1"
        return clean.rstrip("/")

    def _workspace_project_file(self, rel: str, filename: str) -> Path | None:
        try:
            root = (self.toolbox.workspace / ("" if rel == "." else rel)).resolve()
            target = (root / filename).resolve()
            workspace = self.toolbox.workspace.resolve()
            if target == workspace or workspace not in target.parents:
                return None
            return target
        except Exception:
            return None

    def _clean_rel_path(self, path: Any) -> str:
        clean = str(path or ".").strip().replace("\\", "/").strip("/")
        return clean or "."

    def _append_note(self, final_text: str, note: str) -> str:
        clean = (final_text or "").rstrip()
        if "blocked" in note.lower() and clean.startswith("Completed the requested work"):
            clean = clean.replace("Completed the requested work using tool-backed evidence.", "Recorded partial tool-backed progress.", 1)
        if not clean:
            return note
        if note in clean:
            return clean
        return f"{clean}\n\n{note}"
