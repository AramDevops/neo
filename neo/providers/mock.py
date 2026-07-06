from __future__ import annotations

import json
import time

from .base import BaseProvider, ProviderResult


class MockProvider(BaseProvider):
    provider_name = "mock"

    def generate(self, prompt: str, images: list[str] | None = None) -> ProviderResult:
        start = time.perf_counter()
        current = prompt.split("Current user message:")[-1].lower()
        full = prompt.lower()

        if "delete the whole workspace" in current:
            tool_calls = []
            final = "I will not delete the workspace. A safe alternative is to inspect files, make a backup, then remove only approved targets."
        elif "needle-neo-42" in current and "grep" in current:
            if '"tool": "grep"' in full and "needle-neo-42" in full:
                tool_calls = []
                final = "Created probe.txt and grep found needle-neo-42 in diagnostic/probe.txt."
            else:
                tool_calls = [
                    {"tool": "write_file", "args": {"path": "diagnostic/probe.txt", "content": "needle-neo-42\n"}},
                    {"tool": "grep", "args": {"path": "diagnostic", "pattern": "needle-neo-42"}},
                ]
                final = "I will create the probe file and grep for needle-neo-42."
        elif "json_validate" in current:
            if '"tool": "json_validate"' in full:
                tool_calls = []
                final = "The JSON is valid and the engine field is neo."
            else:
                tool_calls = [{"tool": "json_validate", "args": {"text": "{\"engine\":\"Neo\",\"ok\":true,\"score\":7}"}}]
                final = "I will validate the JSON."
        elif "sql_query" in current:
            if '"tool": "sql_query"' in full:
                tool_calls = []
                final = "The read-only SQL query returned harness tables including runs and agents."
            else:
                tool_calls = [{"tool": "sql_query", "args": {"query": "SHOW TABLES"}}]
                final = "I will inspect the harness database with a read-only SQL query."
        elif "artifact-neo-77" in current:
            if '"tool": "write_artifact"' in full:
                tool_calls = []
                final = "The diagnostic artifact was written with token artifact-neo-77."
            else:
                tool_calls = [{"tool": "write_artifact", "args": {"name": "eval_tool_artifact.txt", "content": "artifact-neo-77"}}]
                final = "I will write the diagnostic artifact."
        elif "scrape_page" in current and "example.com" in current:
            if '"tool": "scrape_page"' in full:
                tool_calls = []
                final = "scrape_page read https://example.com and found Example Domain."
            else:
                tool_calls = [{"tool": "scrape_page", "args": {"url": "https://example.com"}}]
                final = "I will scrape https://example.com."
        elif "research_web" in current or "scrape" in current or "read the web" in current or "read pages" in current:
            query = "neo-web-test"
            if "rent" in current:
                query = "montreal rent prices"
            if '"tool": "research_web"' in full:
                tool_calls = []
                final = f"research_web searched and read source pages for {query}."
            else:
                tool_calls = [{"tool": "research_web", "args": {"query": query, "max_pages": 2}}]
                final = f"I will search and scrape source pages for {query}."
        elif "web_search" in current or "search online" in current or "online search" in current:
            query = "neo-web-test"
            if "rent" in current:
                query = "rent"
            if '"tool": "web_search"' in full:
                tool_calls = []
                final = f"web_search ran for query {query}. I found online search candidates in the tool output."
            else:
                tool_calls = [{"tool": "web_search", "args": {"query": query}}]
                final = f"I will run web_search for {query}."
        elif "web_fetch" in current or "example.com" in current:
            if '"tool": "web_fetch"' in full:
                tool_calls = []
                final = "web_fetch inspected https://example.com and found Example Domain content."
            else:
                tool_calls = [{"tool": "web_fetch", "args": {"url": "https://example.com"}}]
                final = "I will fetch https://example.com."
        elif "sava" in current and "brin" in current:
            tool_calls = []
            final = "Sava cannot enter a red room because every brin is a tor, and every tor avoids red rooms."
        elif "sum of squares" in current or "python" in current or "calculate" in current:
            if "5525" in full:
                tool_calls = []
                final = "The sum of squares from 1 to 25 is 5525. Formula: square sum n(n+1)(2n+1)/6."
            else:
                tool_calls = [{"tool": "python", "args": {"code": "print(sum(i*i for i in range(1, 26)))"}}]
                final = "I will calculate the requested value with Python."
        elif "workspace" in current or "inspect" in current or "list" in current:
            if '"tool": "list_files"' in full and '"ok": true' in full:
                tool_calls = []
                final = "The harness workspace is reachable. It may be empty or contain .gitkeep."
            else:
                tool_calls = [{"tool": "list_files", "args": {"path": "."}}]
                final = "I will inspect the harness workspace."
        else:
            tool_calls = []
            final = "Mock response from neo. The system is wired and ready."
        payload = {
            "plan": ["Read shared context", "Use tools if needed", "Return a concise answer"],
            "tool_calls": tool_calls,
            "final": final,
            "needs_more": bool(tool_calls),
        }
        return ProviderResult(
            text=json.dumps(payload),
            model=self.model or "mock",
            provider=self.provider_name,
            latency_ms=int((time.perf_counter() - start) * 1000),
            token_estimate=max(1, len(prompt.split())),
        )
