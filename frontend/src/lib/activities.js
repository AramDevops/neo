export function modelActivity(payload) {
  const text = payload.text || "";
  if (!text) return { plan: [], toolCalls: [], final: "" };
  try {
    const parsed = JSON.parse(text);
    return {
      plan: Array.isArray(parsed.plan) ? parsed.plan.map(String).slice(0, 5) : [],
      toolCalls: Array.isArray(parsed.tool_calls)
        ? parsed.tool_calls.map((call) => call && call.tool).filter(Boolean).slice(0, 4)
        : [],
      final: String(parsed.final || "").slice(0, 500)
    };
  } catch {
    return { plan: [], toolCalls: [], final: text.slice(0, 500) };
  }
}

export function toolActivityTitle(tool) {
  const labels = {
    powershell: "ran command",
    start_process: "started process",
    process_status: "checked process",
    python: "ran python",
    read_file: "read file",
    write_file: "edited file",
    append_file: "edited file",
    edit_file: "edited file",
    make_dir: "created directory",
    move_path: "moved path",
    file_info: "inspected file",
    tree: "listed tree",
    list_files: "listed files",
    search_files: "searched files",
    grep: "searched text",
    python_symbols: "inspected code",
    json_validate: "validated json",
    web_search: "searched web",
    web_fetch: "read web page",
    web_links: "read page links",
    scrape_page: "scraped page",
    scrape_urls: "scraped pages",
    research_web: "researched web",
    http_head: "checked headers",
    http_get: "fetched url",
    open_browser: "opened browser",
    open_vscode: "opened vscode",
    download_url: "downloaded url",
    sql_query: "queried database",
    git_status: "checked git",
    git_diff: "read diff",
    context_read: "read shared context",
    context_write: "wrote shared context",
    list_agents: "listed agents",
    metrics_snapshot: "read metrics",
    tool_catalog: "read tool catalog",
    write_artifact: "wrote artifact",
    screen_capture: "captured screen",
    computer_click: "clicked screen",
    computer_move: "moved pointer",
    computer_type: "typed on screen",
    computer_key: "pressed keys",
    computer_scroll: "scrolled screen",
    list_windows: "listed windows",
    focus_window: "focused window"
  };
  return labels[tool] || String(tool || "used tool").replaceAll("_", " ");
}

export function activityScreenshot(activity) {
  if (activity.type !== "tool_result") return null;
  const payload = activity.payload || {};
  const meta = payload.meta || {};
  const path = String(meta.screenshot_path || "");
  let url = String(meta.screenshot_url || "");
  if (!url && path) {
    // Older events carry only the absolute path; everything under artifacts/
    // is reachable through /api/artifacts/.
    const normalized = path.replace(/\\/g, "/");
    const tail = normalized.split(/\/artifacts\//i).pop();
    if (tail && tail !== normalized) url = `/api/artifacts/${tail}`;
  }
  if (!url) return null;
  const fileName = (path || url).replace(/\\/g, "/").split("/").pop() || "screenshot.png";
  return { url, fileName };
}

export function collectScreenshots(agentActivities) {
  return (agentActivities || []).flatMap((activity) => {
    const shot = activityScreenshot(activity);
    if (!shot) return [];
    return [{
      ...shot,
      activityId: activity.id,
      runId: activity.run_id,
      createdAt: activity.created_at
    }];
  });
}

export function activityTitle(activity) {
  const payload = activity.payload || {};
  if (activity.type === "run_started") return "run started";
  if (activity.type === "model_response") {
    const parsed = modelActivity(payload);
    const toolsText = parsed.toolCalls.length ? ` -> ${parsed.toolCalls.join(", ")}` : "";
    return `thinking${toolsText}`;
  }
  if (activity.type === "ui_pending") return payload.status || "thinking";
  if (activity.type === "context_injected") return "context added";
  if (activity.type === "plan_progress") return "plan updated";
  if (activity.type === "provider_retry") return `retry ${payload.attempt}/${payload.max}`;
  if (activity.type === "provider_retry_failed") return "retry failed";
  if (activity.type === "recovery_attempt") return "recovery attempt";
  if (activity.type === "run_blocked") return "blocked";
  if (activity.type === "run_complete") return "complete";
  if (activity.type === "stop_requested") return "stop requested";
  if (activity.type === "run_stopped") return "stopped by user";
  if (activity.type === "tool_result") {
    const title = toolActivityTitle(payload.tool);
    return payload.ok ? title : `failed ${title}`;
  }
  if (activity.type === "run_error") return "run error";
  return activity.type;
}

export function activityOutput(activity) {
  const payload = activity.payload || {};
  if (activity.type === "run_started") {
    return [
      payload.agent_name ? `agent: ${payload.agent_name}` : "",
      payload.provider && payload.model ? `engine: ${payload.provider}:${payload.model}` : ""
    ].filter(Boolean).join("\n");
  }
  if (activity.type === "model_response") {
    const parsed = modelActivity(payload);
    return [
      parsed.plan.length ? `plan: ${parsed.plan.join(" | ")}` : "plan: none",
      parsed.final ? `draft: ${parsed.final}` : ""
    ].filter(Boolean).join("\n");
  }
  if (activity.type === "ui_pending") return payload.detail || "";
  if (activity.type === "context_injected") return payload.content || "";
  if (activity.type === "plan_progress") return payload.status || "";
  if (activity.type === "provider_retry") return payload.error || payload.message || "";
  if (activity.type === "provider_retry_failed") return payload.error || payload.message || "";
  if (activity.type === "recovery_attempt") return JSON.stringify(payload, null, 2);
  if (activity.type === "run_blocked") return payload.reason || "";
  if (activity.type === "run_complete") return JSON.stringify(payload, null, 2);
  if (activity.type === "stop_requested") return "Stop requested. The run ends at its next checkpoint; a tool call in flight finishes first.";
  if (activity.type === "run_stopped") return JSON.stringify(payload, null, 2);
  const output = payload.output ?? payload.error ?? "";
  return typeof output === "string" ? output : JSON.stringify(output, null, 2);
}

export function activityArgs(activity) {
  const payload = activity.payload || {};
  if (["run_started", "model_response", "ui_pending", "context_injected", "plan_progress"].includes(activity.type) || !payload.args || Object.keys(payload.args).length === 0) return "";
  return JSON.stringify(payload.args, null, 2);
}

export function activityFileChange(activity) {
  const payload = activity.payload || {};
  const meta = payload.meta || {};
  const changedTools = new Set(["write_file", "append_file", "edit_file", "make_dir", "move_path", "download_url"]);
  const hasChangeMeta = Number(meta.files_changed || 0) > 0 || meta.added !== undefined || meta.removed !== undefined;
  if (activity.type !== "tool_result" || (!changedTools.has(payload.tool) && !hasChangeMeta)) return null;
  const relativePath = meta.relative_path || meta.final_path || meta.path || payload.args?.path || payload.args?.destination || "";
  if (!relativePath) return null;
  return {
    relativePath,
    fileName: meta.file_name || relativePath.split(/[\\/]+/).pop(),
    filesChanged: Number(meta.files_changed || 1),
    added: Number(meta.added || 0),
    removed: Number(meta.removed || 0),
    tool: payload.tool
  };
}
