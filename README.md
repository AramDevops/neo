# Neo

**English** | [Français](README.fr.md)

**An agent harness that makes small, low-cost models reliable on real software tasks.**

![Neo: small models building and running a full CRM app in parallel](imgs/neo-demo.gif)

Neo's premise is that reliability comes from the harness, not the model. Most agent failures are not bad code; they are operational: starting a server on an occupied port, reporting "done" without checking, repeating a failed command instead of reading the error. Neo places that discipline in deterministic code, so small, fast models (Gemini Flash, local models via Ollama) can complete tasks that usually require a frontier model: build and run web apps, fix broken ones, operate the desktop, read documents, and prove each result before reporting success.

## The core idea

```
 user request (any language)
        |
        v
 1. UNDERSTAND   one small model call classifies intent into a strict schema
        |        {kind, wants_changes, needs_running_app}
        v
 2. EXECUTE      deterministic pipelines drive the work
        |        probe -> install -> port -> start -> verify -> open
        v
 3. PROVE        outcome checks decide success, never the model's word
                 HTTP status, MIME types, dependency scans, acceptance checks
```

The model fills content gaps (write this file, choose this coordinate); sequencing, verification, and recovery are deterministic code. A run cannot report success without evidence: a process that actually listens, an HTTP check that passes, a health check that covers the requested feature. A run that changes nothing but claims completion is blocked and returned.

## Why it works with weaker models

Small models fail at agent work in predictable, non-code ways. Neo handles each in the harness:

- **Missed sequencing.** They type before focusing a field, start a server before installing, report done before verifying. Composite tools collapse click-then-type into one action, and the runtime enforces probe -> install -> port -> start -> verify -> open regardless of the order requested.
- **Unverified claims.** "The app is running" is not evidence. Neo accepts only a listening port, a 200 response, or a passing health check; an unsupported claim is returned with the evidence attached.
- **Failure loops.** On error, small models repeat the command or apologize. Neo feeds stderr back into the prompt, fixes deterministic failures in the tool itself (a stale lockfile falls back to `npm install`), and auto-retries blocked runs under an attempt cap.
- **Weak visual grounding.** Pixel-accurate clicking is a frontier-model skill. Neo overlays a labeled coordinate grid so the model reads a position instead of estimating one, and launches desktop apps directly.
- **Task drift.** Long tasks scatter small models across unrelated projects. Intent classification fixes what the request is, project targeting fixes what it concerns, and the verdict withholds completion until that specific target works.

Neo does not make the model smarter. It narrows the model's job to filling content and makes everything that requires discipline deterministic. That is why a model costing a fraction of a frontier one completes the same tasks here.

## What's inside

- **Multi-agent terminals**: run several agents side by side with shared context, each on its own provider/model. Flask backend, React frontend.
- **Providers**: Gemini, OpenAI, Anthropic, or any OpenAI-compatible local endpoint (Ollama, LM Studio). Switch per-agent from the UI.
- **60+ sandboxed tools** for files, shell (PowerShell/WSL), Python, git, HTTP, web search and scraping, SQL, document extraction (PDF/Word/Excel), security audits, process management.
- **Real verification**: `app_healthcheck` tests a served app as a user would: the root page, every referenced css/js with correct MIME types, missing Node dependencies detected before startup, plus declared acceptance checks (POST /login with admin/admin, expect "welcome") that the harness executes itself.
- **Intent classification** by a model, not regex, so French, Arabic, typos, and pasted error logs route correctly. Keyword rules remain only as a fallback.
- **Computer use for weak models**: screenshots return with a labeled coordinate grid so the model reads real pixel positions instead of guessing. Composite actions (`computer_type_at` = click, wait for focus, type) remove the motor sequencing small models fumble. `open_app` launches desktop apps directly.
- **Process manager** that distinguishes "started" from "serving": a process counts as running only when its port listens or it survives a startup window; crashes carry stderr evidence back to the model. The registry persists to disk, so orphaned servers from a previous session can be reclaimed.
- **Auto-recovery**: blocked runs retry with the failure evidence injected, bounded by an attempt cap and a no-progress guard.
- **Self-healing tools**: deterministic failures are fixed in the tool itself. `npm ci` with a stale lockfile falls back to `npm install`, npm's Windows `.cmd` shims resolve correctly, occupied ports are reclaimed when Neo owns the listener.
- **Permission modes**: computer control is gated, via ask mode with timed grants or full control.
- **Slash commands**: `/ls`, `/tree`, `/grep`, `/model`, `/status` and others run directly against the backend, with no model call.
- **A benchmark that cannot be gamed.** Deterministic scenarios run through the real runner, tools, and processes, graded on database and filesystem evidence rather than model text. They include a model that lies (must end blocked), a no-op run that claims unmade changes, and a failing acceptance check that no prose can bypass. False-success rate is the primary metric, and it remains zero.

## Quickstart

Requirements: Python 3.11+, Node 18+, and at least one model API key (or a local OpenAI-compatible server).

```bash
git clone https://github.com/AramDevops/neo.git
cd neo

# backend
python -m venv venv
venv/Scripts/pip install -r requirements.txt        # Windows
# venv/bin/pip install -r requirements.txt          # Linux/macOS

# frontend
cd frontend
npm install
npm run build
cd ..

# config
cp .env.example .env      # then put your API key(s) in .env

# run
venv/Scripts/python app.py
```

Open `http://127.0.0.1:8791`, create a terminal, pick a provider, and enter a task such as *"build a markdown editor app and run it"*.

For frontend development, run `npm run dev` in `frontend/` (serves on 8792, proxies `/api` to the backend).

### Configuration

Everything is set in `.env` (see `.env.example` for the full list):

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | provider keys; set at least one |
| `NEO_PROVIDER`, `NEO_MODEL` | default provider and model for new agents |
| `NEO_LOCAL_BASE_URL`, `NEO_LOCAL_MODELS` | OpenAI-compatible local endpoint (Ollama etc.) |
| `NEO_DB_DRIVER` | `sqlite` (default, zero setup) or `mysql` |
| `NEO_MAX_AGENT_LOOPS` | tool-call loops per run (default 12) |
| `NEO_AUTO_RECOVERY_MAX` | bounded auto-retries for blocked runs (default 2) |

Optional extras for desktop control and documents: `pyautogui`, `mss`, `Pillow`, `pypdf`, `python-docx`, `openpyxl` (all in `requirements.txt`).

## How it performs

A harness is only as good as the real work it ships. So Neo is measured on [SWE-bench Verified](https://www.swebench.com/), the industry-standard coding-agent benchmark: real GitHub issues from real projects, where a fix counts only when the project's own hidden test suite passes. No string matching, no model grading itself.

![Neo on SWE-bench Verified](imgs/swebench_results.png)

Across 12 real issues, **`gemini-pro-latest` resolves 9 (75%)** with Neo's test-feedback loop - it patches, sees which of the project's real tests still fail, and iterates until they pass. `gpt-5` resolves 7, and the efficient `gemini-flash` tiers land 5 and 3. Every fix is verified by the project's own tests; when Neo can't crack an issue it ships no patch, never a plausible fake.

**The harness sharpens itself.** Running many models exposes where *Neo* is the bottleneck, not the model. This work surfaced two real harness bugs - a browser check wrongly firing on pure code fixes, and single-shot patching with no way to recover from a failing test. Fixing them (the test-feedback loop) lifted `gemini-pro` from 6/12 to 9/12 and `gpt-5` from 3 to 7. A benchmark that upgrades the tool it measures.

Neo edits a checkout of the repo on the host, produces a git diff, and the diff is graded in a sealed Docker container by the repo's real tests.

For a fast model smoke test, Neo also ships an internal eval:

```bash
pip install matplotlib   # once, for the charts
python -m neo.services.model_compare --models gemini-flash-lite-latest,gemini-flash-latest,gemini-pro-latest
```

Add `--demo` to preview the format without any API call.

## Status and roadmap

Neo is early and moving quickly. Windows is the primary target today (the shell and desktop-control tools depend on PowerShell and the Win32 desktop); the core harness, tools, and verification run anywhere Python does. Planned:

- richer acceptance-check types (DOM assertions, screenshot diffing)
- a task-graph state machine for long multi-step builds
- more benchmark scenarios and languages
- first-class Linux/macOS desktop control

Contributions are welcome. Open an issue for anything surprising, or a PR if you have already fixed it. Please run the benchmark before and after your change.

## License

MIT, see [LICENSE](LICENSE).

Built by [Akram Nasr](https://github.com/AramDevops).
