from __future__ import annotations

"""Deterministic app outcome verification.

The fundamental fix for "the harness verifies the SHAPE of the work, not the
SUBSTANCE": plan-step matching can only prove that a write-ish or start-ish
tool fired, so a shallow run (run 120: zero file edits, one restart) could
still be stamped complete. This engine tests the actual running app:

- baseline: the root page serves, and every same-host css/js it references
  serves with a sane content type (an SPA fallback answering text/html for
  style.css renders a blank page while every naive 200-check passes);
- declared acceptance checks: the model DECLARES what success looks like
  ({method, path, body, expect_contains}) and the harness EXECUTES it; the
  model can propose evidence, never certify it;
- static Node dependency gaps: a require()'d package that is neither a
  builtin nor installed in node_modules is reported BEFORE the app is even
  started (the body-parser class of failure, known in advance).

The report is structured so a weak model can fix exactly what failed.
"""

import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List


FETCH_TIMEOUT_SECONDS = 8.0
MAX_BODY_BYTES = 200_000
MAX_ASSET_CHECKS = 6
MAX_CUSTOM_CHECKS = 8
MAX_SCANNED_JS_FILES = 40

_ASSET_REF_PATTERN = re.compile(r"(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", re.I)
_ASSET_SUFFIXES = (".css", ".js", ".mjs")

# Node builtin modules: require()-able without an install.
NODE_BUILTINS = frozenset({
    "assert", "buffer", "child_process", "cluster", "console", "constants", "crypto",
    "dgram", "dns", "domain", "events", "fs", "http", "http2", "https", "inspector",
    "module", "net", "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "timers", "tls", "trace_events",
    "tty", "url", "util", "v8", "vm", "worker_threads", "zlib",
})

_REQUIRE_PATTERN = re.compile(r"require\(\s*[\"']([^\"']+)[\"']\s*\)")
_IMPORT_PATTERN = re.compile(r"(?:^|\n)\s*import\s+(?:[^\n\"']+\s+from\s+)?[\"']([^\"']+)[\"']")


def fetch(url: str, method: str = "GET", body: str | None = None) -> tuple[int, str, str]:
    """Bounded HTTP fetch. Returns (status, content_type, text); status 0 means
    the connection itself failed and text carries the error."""
    data = body.encode("utf-8") if isinstance(body, str) else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            text = response.read(MAX_BODY_BYTES).decode("utf-8", errors="replace")
            return int(response.status), str(response.headers.get("Content-Type") or ""), text
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(MAX_BODY_BYTES).decode("utf-8", errors="replace")
        except Exception:
            text = ""
        content_type = str(exc.headers.get("Content-Type") or "") if exc.headers else ""
        return int(exc.code), content_type, text
    except Exception as exc:
        return 0, "", str(exc)


def sanitize_checks(raw: Any) -> List[Dict[str, Any]]:
    """Normalize model-declared acceptance checks into a strict shape."""
    checks: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return checks
    for item in raw[:MAX_CUSTOM_CHECKS]:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method") or "GET").strip().upper()
        if method not in {"GET", "POST"}:
            method = "GET"
        path = str(item.get("path") or "/").strip()
        if not path.startswith("/"):
            path = "/" + path
        check: Dict[str, Any] = {"method": method, "path": path}
        body = item.get("body")
        if isinstance(body, str) and body:
            check["body"] = body[:4000]
        expect_contains = item.get("expect_contains")
        if isinstance(expect_contains, str) and expect_contains:
            check["expect_contains"] = expect_contains[:400]
        try:
            expect_status = int(item.get("expect_status"))
            if 100 <= expect_status <= 599:
                check["expect_status"] = expect_status
        except (TypeError, ValueError):
            pass
        checks.append(check)
    return checks


def run_checks(url: str, checks: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    """Run baseline + declared checks against a served URL. Returns a
    structured report: {ok, results: [{name, ok, detail}], summary}."""
    results: List[Dict[str, Any]] = []
    base = url.rstrip("/")

    status, content_type, text = fetch(base + "/" if not urllib.parse.urlparse(base).path else base)
    root_ok = 200 <= status < 400
    results.append({
        "name": "root",
        "ok": root_ok,
        "detail": f"{status or 'connection failed'} {content_type}".strip() if root_ok else (
            f"status {status}: {text[:160]}" if status else f"connection failed: {text[:160]}"
        ),
    })

    if root_ok:
        page_host = urllib.parse.urlparse(base).netloc
        refs: List[str] = []
        for raw in _ASSET_REF_PATTERN.findall(text or ""):
            clean = raw.strip()
            if not clean or clean.startswith(("#", "data:", "mailto:", "javascript:")):
                continue
            if clean.split("?")[0].lower().endswith(_ASSET_SUFFIXES) and clean not in refs:
                refs.append(clean)
        for ref in refs[:MAX_ASSET_CHECKS]:
            asset_url = urllib.parse.urljoin(base + "/", ref)
            if urllib.parse.urlparse(asset_url).netloc != page_host:
                continue
            asset_status, asset_type, asset_text = fetch(asset_url)
            asset_ok = 200 <= asset_status < 400 and "text/html" not in asset_type.lower()
            if asset_ok:
                detail = f"{asset_status} {asset_type}".strip()
            elif not asset_status:
                detail = f"connection failed: {asset_text[:120]}"
            elif "text/html" in asset_type.lower():
                detail = f"served as {asset_type} - static files are not being served (SPA fallback / wrong static dir)"
            else:
                detail = f"status {asset_status}"
            results.append({"name": f"asset:{ref}", "ok": asset_ok, "detail": detail})

    for check in sanitize_checks(checks):
        target = urllib.parse.urljoin(base + "/", check["path"].lstrip("/"))
        status, content_type, text = fetch(target, check["method"], check.get("body"))
        expected_status = check.get("expect_status")
        status_ok = (status == expected_status) if expected_status else (200 <= status < 400)
        contains = check.get("expect_contains")
        contains_ok = (contains.lower() in text.lower()) if contains else True
        ok = bool(status_ok and contains_ok)
        pieces = [f"status {status or 'connection failed'}"]
        if contains:
            pieces.append(f"'{contains}' {'found' if contains_ok else 'NOT found'} in response")
        if not ok and text:
            pieces.append(f"body starts: {text[:120]!r}")
        results.append({
            "name": f"{check['method']} {check['path']}",
            "ok": ok,
            "detail": "; ".join(pieces),
        })

    failed = [item for item in results if not item["ok"]]
    summary = (
        f"{len(results) - len(failed)}/{len(results)} checks passed"
        + ("" if not failed else "; FAILING: " + "; ".join(f"{item['name']} ({item['detail']})" for item in failed[:5]))
    )
    return {"ok": not failed, "results": results, "summary": summary, "url": base}


def node_dependency_gaps(project_dir: Path) -> List[str]:
    """Packages require()'d/imported by the project's JS that are neither Node
    builtins nor present in node_modules: startup failures knowable BEFORE
    start ("Cannot find module 'body-parser'")."""
    root = Path(project_dir)
    if not root.is_dir():
        return []
    modules_dir = root / "node_modules"
    gaps: set[str] = set()
    scanned = 0
    stack = [root]
    while stack and scanned < MAX_SCANNED_JS_FILES:
        current = stack.pop(0)
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if child.name not in {"node_modules", ".git", "dist", "build", ".neo"}:
                    stack.append(child)
                continue
            if child.suffix.lower() not in {".js", ".mjs", ".cjs"} or scanned >= MAX_SCANNED_JS_FILES:
                continue
            scanned += 1
            try:
                text = child.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for name in _REQUIRE_PATTERN.findall(text) + _IMPORT_PATTERN.findall(text):
                clean = name.strip()
                if not clean or clean.startswith((".", "/")):
                    continue  # relative/local imports are files, not packages
                if clean.startswith("node:"):
                    clean = clean[len("node:"):]
                base = clean.split("/")[0] if not clean.startswith("@") else "/".join(clean.split("/")[:2])
                if base in NODE_BUILTINS:
                    continue
                if (modules_dir / base).exists():
                    continue
                gaps.add(base)
    return sorted(gaps)
