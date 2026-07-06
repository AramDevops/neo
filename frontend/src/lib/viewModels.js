import { normalizeModelList } from "./providerModels.js";

export function metricRows(metrics) {
  const total = Number(metrics.runs_total || 0);
  const complete = Number(metrics.runs_complete || 0);
  const completionPct = total ? Math.min(100, Math.max(0, Math.round((complete / total) * 100))) : 0;
  return [
    { label: "runs", value: total },
    { label: "complete", value: `${complete}/${total}`, raw: completionPct },
    { label: "avg ms", value: metrics.avg_latency_ms || 0 },
    { label: "tools", value: metrics.tool_calls || 0 },
    { label: "tokens", value: metrics.token_estimate || 0 }
  ];
}

export function dbLabel(config) {
  return `${config.db_driver || "db"}:${config.mysql_database || "local"}`;
}

export function normalizeAgentText(value, agentNameById) {
  return String(value || "")
    .replace(/\bAgent\s+(\d+)\b/g, (match, id) => agentNameById.get(Number(id)) || match)
    .replace(/\bagent:(\d+)\b/g, (match, id) => {
      const name = agentNameById.get(Number(id));
      return name ? `agent:${name}` : match;
    });
}

export function runAgentLabel(run, agentNameById) {
  return run.agent_name || agentNameById.get(Number(run.agent_id)) || run.agent_id || "-";
}

export function workspaceLabel(workspace) {
  const full = workspace.workspace_dir || "";
  if (!full) return "workdir unset";
  const parts = full.split(/[\\/]+/).filter(Boolean);
  if (parts.length <= 2) return full;
  return `wd:${parts.slice(-2).join("/")}`;
}

export function shortTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function providersFromCatalog(modelCatalog) {
  return modelCatalog.providers?.length
    ? modelCatalog.providers
    : [{ id: "gemini", label: "Gemini", status: "unknown", default_model: "gemini-3.5-flash", models: ["gemini-3.5-flash"], custom_model: true }];
}

export function providerInfoFromList(providerList, providerId) {
  return providerList.find((item) => item.id === providerId) || providerList[0];
}

export function providerNeedsApiKey(providerId) {
  return providerId !== "local";
}

export function providerShowsEndpoint(providerId) {
  return ["local", "openai"].includes(providerId);
}

export function modelOptionsForProvider(info, model) {
  const options = normalizeModelList(info?.models || []);
  return options.includes(model) || !model ? options : normalizeModelList([model, ...options]);
}

export function friendlyProviderError(error, fallback) {
  const text = String(error?.message || fallback || "provider sync failed");
  if (/not found/i.test(text)) return "manual models cached";
  if (/api key/i.test(text)) return text;
  if (/failed to fetch|network/i.test(text)) return "endpoint pending";
  return fallback || text;
}

export function groupTools(tools) {
  const byName = new Map(tools.map((tool) => [tool.name, tool]));
  const used = new Set();
  const explicitBuckets = [
    { title: "wsl / linux", names: ["wsl_status", "wsl_probe", "wsl"] },
    { title: "setup + install", category: "setup" },
    { title: "runtime", category: "runtime" },
    { title: "web + scraping", category: "network" },
    { title: "workspace files", category: "workspace" },
    { title: "security", category: "security" },
    { title: "diagnostics", category: "diagnostics" },
    { title: "coordination", category: "coordination" },
    { title: "external apps", category: "external" },
    { title: "shell", category: "shell" }
  ];
  const groups = explicitBuckets.map((bucket) => {
    const items = bucket.names
      ? bucket.names.map((name) => byName.get(name)).filter(Boolean)
      : tools.filter((tool) => tool.category === bucket.category && !used.has(tool.name));
    items.forEach((tool) => used.add(tool.name));
    return { title: bucket.title, items };
  }).filter((group) => group.items.length);
  const legacyBuckets = [
    { title: "web + scraping", names: ["web_search", "web_fetch", "web_links", "scrape_page", "scrape_urls", "research_web", "http_head", "http_get", "open_browser", "download_url"] },
    { title: "workspace files", names: ["read_file", "write_file", "append_file", "edit_file", "make_dir", "move_path", "file_info", "tree", "list_files", "search_files", "grep", "python_symbols", "json_validate"] },
    { title: "execution", names: ["powershell", "start_process", "process_status", "python", "open_vscode"] },
    { title: "database + context", names: ["sql_query", "context_read", "context_write", "list_agents", "metrics_snapshot"] },
    { title: "diagnostics", names: ["git_status", "git_diff", "tool_catalog", "write_artifact"] }
  ];
  legacyBuckets.forEach((bucket) => {
    const items = bucket.names.map((name) => byName.get(name)).filter(Boolean);
    const fresh = items.filter((tool) => !used.has(tool.name));
    if (!fresh.length) return;
    items.forEach((tool) => used.add(tool.name));
    groups.push({ title: bucket.title, items: fresh });
  });
  const other = tools.filter((tool) => !used.has(tool.name));
  if (other.length) groups.push({ title: "other", items: other });
  return groups;
}
