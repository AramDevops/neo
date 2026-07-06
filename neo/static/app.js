window.neoApp = function neoApp() {
  return {
    agents: [],
    messages: {},
    activities: {},
    plans: {},
    runs: [],
    evalRuns: [],
    sharedContext: [],
    tools: [],
    metrics: {},
    config: {},
    workspace: {},
    hydratedConfig: false,
    provider: "gemini",
    model: "gemini-3.5-flash",
    cols: 2,
    rowHeight: 420,
    selectedRunId: null,
    traceText: "select a run",
    syncActive: JSON.parse(localStorage.getItem("neo.syncActive") || "true"),
    syncTimer: null,
    settingsOpen: false,
    settingsTab: "engine",
    workspaceDraft: "",
    workspaceDirty: false,
    workspaceError: "",
    contextDraft: "",
    drafts: {},
    typedMessages: {},
    typingTimers: {},
    messageSigs: {},
    expandedActivities: {},
    transcriptPinned: {},
    hiddenPlans: JSON.parse(localStorage.getItem("neo.hiddenPlans") || "{}"),
    hiddenAgents: JSON.parse(localStorage.getItem("neo.hiddenAgents") || "{}"),
    accents: ["#71d88a", "#6fc7d8", "#e0bd64", "#b59cff", "#e07171", "#9cd67a"],

    async api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    },

    async boot() {
      window.addEventListener("resize", () => this.syncGeometry());
      await this.loadState();
      await this.ensureDefaultAgents();
      await this.loadState();
      await this.loadMessages(true);
      this.$nextTick(() => {
        this.syncGeometry();
        this.scrollTranscripts(true);
      });
      this.syncSyncTimer();
    },

    async refresh(forceMessages = false) {
      await this.loadState();
      await this.loadMessages(forceMessages);
      this.$nextTick(() => {
        this.syncGeometry();
        this.scrollTranscripts(false);
      });
    },

    syncSyncTimer() {
      if (this.syncTimer) {
        clearInterval(this.syncTimer);
        this.syncTimer = null;
      }
      localStorage.setItem("neo.syncActive", JSON.stringify(this.syncActive));
      if (!this.syncActive) return;
      this.refresh(false).catch(() => {});
      this.syncTimer = setInterval(() => this.refresh(false).catch(() => {}), 4000);
    },

    toggleSync() {
      this.syncActive = !this.syncActive;
      this.syncSyncTimer();
    },

    async loadState() {
      const data = await this.api("/api/state");
      this.agents = data.agents || [];
      this.runs = data.runs || [];
      this.evalRuns = data.eval_runs || [];
      this.sharedContext = data.shared_context || [];
      this.metrics = data.metrics || {};
      this.config = data.config || {};
      this.workspace = data.workspace || {};
      this.tools = data.tools || [];
      if (!this.workspaceDirty) {
        this.workspaceDraft = this.workspace.workspace_dir || "";
      }
      if (!this.hydratedConfig) {
        this.provider = this.config.provider || this.provider;
        this.model = this.config.model || this.model || "gemini-3.5-flash";
        this.hydratedConfig = true;
      }
    },

    async ensureDefaultAgents() {
      const missing = Math.max(0, 4 - this.agents.length);
      for (let i = 0; i < missing; i += 1) {
        await this.api("/api/agents", {
          method: "POST",
          body: JSON.stringify({ provider: this.provider, model: this.model }),
        });
      }
    },

    agentSig(agent) {
      return [agent.id, agent.status, agent.updated_at || "", agent.provider, agent.model].join("|");
    },

    async loadMessages(force = false) {
      await Promise.all(this.agents.map(async (agent) => {
        const sig = this.agentSig(agent);
        const shouldLoad = force || !this.messages[agent.id] || agent.status === "running" || this.messageSigs[agent.id] !== sig;
        if (!shouldLoad) return;
        const firstLoad = !this.messages[agent.id];
        const data = await this.api(`/api/agents/${agent.id}/messages`);
        const nextMessages = data.messages || [];
        this.prepareTyping(nextMessages, firstLoad);
        this.messages[agent.id] = nextMessages;
        this.activities[agent.id] = data.activities || [];
        this.plans[agent.id] = data.plans || [];
        this.messageSigs[agent.id] = sig;
      }));
    },

    messageKey(message) {
      return String(message.id || `${message.role}-${message.created_at || ""}-${message.content?.length || 0}`);
    },

    prepareTyping(messages, firstLoad) {
      for (const message of messages) {
        if (message.role !== "assistant") continue;
        const key = this.messageKey(message);
        if (this.typedMessages[key] !== undefined) continue;
        if (firstLoad) {
          this.typedMessages[key] = message.content || "";
        } else {
          this.startTyping(message);
        }
      }
    },

    startTyping(message) {
      const key = this.messageKey(message);
      const full = message.content || "";
      if (this.typingTimers[key]) return;
      this.typedMessages[key] = "";
      let index = 0;
      this.typingTimers[key] = setInterval(() => {
        index = Math.min(full.length, index + 4);
        this.typedMessages[key] = full.slice(0, index);
        if (index >= full.length) {
          clearInterval(this.typingTimers[key]);
          delete this.typingTimers[key];
        }
      }, 18);
    },

    displayMessage(message) {
      if (message.role !== "assistant") return message.content || "";
      const key = this.messageKey(message);
      return this.typedMessages[key] ?? message.content ?? "";
    },

    escapeHtml(value) {
      return String(value || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    },

    parseSources(content) {
      const text = String(content || "");
      const match = text.match(/\n\s*Sources:\s*\n/i);
      if (!match) return { body: text, sources: [] };
      const body = text.slice(0, match.index).trimEnd();
      const sourceText = text.slice(match.index + match[0].length);
      const sources = sourceText.split(/\n+/).map((line) => {
        const cleaned = line.replace(/^\s*[-*]\s*/, "").trim();
        const urlMatch = cleaned.match(/https?:\/\/\S+/);
        if (!urlMatch) return null;
        const url = urlMatch[0].replace(/[),.;]+$/, "");
        const title = cleaned.slice(0, urlMatch.index).replace(/:\s*$/, "").trim() || url;
        return { title, url };
      }).filter(Boolean);
      return { body, sources };
    },

    sourceDomain(url) {
      try {
        return new URL(url).hostname.replace(/^www\./, "");
      } catch (_) {
        return url;
      }
    },

    faviconUrl(url) {
      try {
        const domain = new URL(url).hostname;
        return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=32`;
      } catch (_) {
        return "";
      }
    },

    renderMessageHtml(message) {
      const parsed = this.parseSources(this.displayMessage(message));
      let html = `<span class="line-text">${this.escapeHtml(parsed.body)}</span>`;
      if (!parsed.sources.length) return html;
      html += '<span class="source-list">';
      for (const source of parsed.sources) {
        const icon = this.faviconUrl(source.url);
        const title = this.escapeHtml(source.title);
        const url = this.escapeHtml(source.url);
        const domain = this.escapeHtml(this.sourceDomain(source.url));
        html += `<a class="source-link" href="${url}" target="_blank" rel="noreferrer" title="${url}">`;
        html += icon ? `<img class="source-icon" src="${this.escapeHtml(icon)}" alt="">` : '<span class="source-icon fallback">&gt;</span>';
        html += `<span class="source-meta"><span class="source-title">${title}</span><span class="source-domain">${domain}</span></span></a>`;
      }
      html += "</span>";
      return html;
    },

    terminalItems(agentId) {
      const messages = (this.messages[agentId] || []).map((message) => ({
        kind: "message",
        key: `message-${message.id}`,
        created_at: message.created_at,
        order: message.role === "assistant" ? 30 : 10,
        message,
      }));
      const activities = (this.activities[agentId] || []).map((activity) => ({
        kind: "activity",
        key: `activity-${activity.id}`,
        created_at: activity.created_at,
        order: activity.type === "model_response" ? 15 : activity.type === "tool_result" ? 20 : 25,
        activity,
      }));
      return [...messages, ...activities].sort((a, b) => {
        const timeA = Date.parse(a.created_at || "") || 0;
        const timeB = Date.parse(b.created_at || "") || 0;
        if (timeA !== timeB) return timeA - timeB;
        if (a.order !== b.order) return a.order - b.order;
        return String(a.key).localeCompare(String(b.key));
      });
    },

    activityTitle(activity) {
      const payload = activity.payload || {};
      if (activity.type === "model_response") {
        const parsed = this.modelActivity(payload);
        const tools = parsed.toolCalls.length ? ` -> ${parsed.toolCalls.join(", ")}` : "";
        return `thinking${tools}`;
      }
      if (activity.type === "tool_result") {
        const title = this.toolActivityTitle(payload.tool);
        return `${payload.ok ? title : `failed ${title}`}`;
      }
      if (activity.type === "run_complete") {
        return `run complete ${payload.latency_ms || 0}ms / tools:${payload.tool_count || 0}`;
      }
      if (activity.type === "run_error") return "run error";
      return activity.type;
    },

    toggleActivity(key) {
      this.expandedActivities[key] = !this.expandedActivities[key];
    },

    activityArgs(activity) {
      const payload = activity.payload || {};
      if (activity.type === "model_response") return "";
      if (!payload.args || Object.keys(payload.args).length === 0) return "";
      return JSON.stringify(payload.args, null, 2);
    },

    activityOutput(activity) {
      const payload = activity.payload || {};
      if (activity.type === "model_response") {
        const parsed = this.modelActivity(payload);
        const plan = parsed.plan.length ? `plan: ${parsed.plan.join(" | ")}` : "plan: none";
        const final = parsed.final ? `draft: ${parsed.final}` : "";
        return [plan, final].filter(Boolean).join("\n");
      }
      if (activity.type === "run_complete") return JSON.stringify(payload, null, 2);
      const output = payload.output ?? payload.error ?? "";
      if (typeof output === "string") return output;
      return JSON.stringify(output, null, 2);
    },

    modelActivity(payload) {
      const fallback = { plan: [], toolCalls: [], final: "" };
      const text = payload.text || "";
      if (!text) return fallback;
      try {
        const parsed = JSON.parse(text);
        const plan = Array.isArray(parsed.plan) ? parsed.plan.map(String).slice(0, 5) : [];
        const toolCalls = Array.isArray(parsed.tool_calls)
          ? parsed.tool_calls.map((call) => call && call.tool).filter(Boolean).slice(0, 4)
          : [];
        return { plan, toolCalls, final: String(parsed.final || "").slice(0, 500) };
      } catch (_) {
        return { plan: [], toolCalls: [], final: text.slice(0, 500) };
      }
    },

    toolActivityTitle(tool) {
      const labels = {
        powershell: "ran command",
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
      };
      return labels[tool] || String(tool || "used tool").replaceAll("_", " ");
    },

    visibleAgents() {
      return this.agents.filter((agent) => !this.hiddenAgents[agent.id]);
    },

    hiddenCount() {
      return this.agents.length - this.visibleAgents().length;
    },

    gridStyle() {
      return `--cols:${this.cols};--terminal-row-height:${this.rowHeight}px`;
    },

    syncGeometry() {
      this.$nextTick(() => {
        const grid = this.$refs.grid;
        if (!grid) return;
        const width = grid.clientWidth || 0;
        const gap = 1;
        const raw = this.cols > 0 ? Math.floor((width - ((this.cols - 1) * gap)) / this.cols) : 420;
        this.rowHeight = Math.max(340, Math.min(raw, 460));
      });
    },

    accent(index) {
      return this.accents[index % this.accents.length];
    },

    planState(agentId) {
      const list = this.plans[agentId] || [];
      const active = list.find((plan) => plan.status === "in_progress");
      return active?.status || list[0]?.status || "idle";
    },

    metricRows() {
      const total = Number(this.metrics.runs_total || 0);
      const complete = Number(this.metrics.runs_complete || 0);
      const completionPct = total ? Math.round((complete / total) * 100) : 0;
      return [
        { label: "runs", value: total },
        { label: "complete", value: `${complete}/${total}`, raw: completionPct },
        { label: "avg ms", value: this.metrics.avg_latency_ms || 0 },
        { label: "tools", value: this.metrics.tool_calls || 0 },
        { label: "tokens", value: this.metrics.token_estimate || 0 },
      ];
    },

    dbLabel() {
      return `${this.config.db_driver || "db"}:${this.config.mysql_database || "local"}`;
    },

    workspaceLabel() {
      const full = this.workspace.workspace_dir || "";
      if (!full) return "workdir unset";
      const parts = full.split(/[\\/]+/).filter(Boolean);
      if (parts.length <= 2) return full;
      return `wd:${parts.slice(-2).join("/")}`;
    },

    openSettings(tab = "engine") {
      this.settingsTab = tab;
      this.settingsOpen = true;
    },

    toolGroups() {
      const buckets = [
        { title: "web + scraping", names: ["web_search", "web_fetch", "web_links", "scrape_page", "scrape_urls", "research_web", "http_head", "http_get", "open_browser", "download_url"] },
        { title: "workspace files", names: ["read_file", "write_file", "append_file", "edit_file", "make_dir", "move_path", "file_info", "tree", "list_files", "search_files", "grep", "python_symbols", "json_validate"] },
        { title: "execution", names: ["powershell", "wsl_status", "wsl", "start_process", "python", "open_vscode"] },
        { title: "database + context", names: ["sql_query", "context_read", "context_write", "list_agents", "metrics_snapshot"] },
        { title: "diagnostics", names: ["git_status", "git_diff", "tool_catalog", "write_artifact"] },
      ];
      const byName = new Map(this.tools.map((tool) => [tool.name, tool]));
      const used = new Set();
      const groups = buckets.map((bucket) => {
        const items = bucket.names.map((name) => byName.get(name)).filter(Boolean);
        items.forEach((tool) => used.add(tool.name));
        return { title: bucket.title, items };
      }).filter((group) => group.items.length);
      const other = this.tools.filter((tool) => !used.has(tool.name));
      if (other.length) groups.push({ title: "other", items: other });
      return groups;
    },

    shortTime(value) {
      if (!value) return "";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    },

    togglePlan(agentId) {
      this.hiddenPlans[agentId] = !this.hiddenPlans[agentId];
      localStorage.setItem("neo.hiddenPlans", JSON.stringify(this.hiddenPlans));
      this.syncGeometry();
    },

    hideAgent(agentId) {
      this.hiddenAgents[agentId] = true;
      localStorage.setItem("neo.hiddenAgents", JSON.stringify(this.hiddenAgents));
      this.syncGeometry();
    },

    showAllAgents() {
      this.hiddenAgents = {};
      localStorage.setItem("neo.hiddenAgents", "{}");
      this.syncGeometry();
    },

    async createAgent() {
      await this.api("/api/agents", {
        method: "POST",
        body: JSON.stringify({ provider: this.provider, model: this.model }),
      });
      await this.refresh(true);
    },

    async setWorkspace() {
      const path = this.workspaceDraft.trim();
      if (!path) {
        this.workspaceError = "Workspace path is required.";
        return;
      }
      this.workspaceError = "";
      try {
        this.workspace = await this.api("/api/workspace", {
          method: "POST",
          body: JSON.stringify({ path, create: true }),
        });
        this.workspaceDraft = this.workspace.workspace_dir || path;
        this.workspaceDirty = false;
        await this.refresh(false);
      } catch (error) {
        this.workspaceError = error.message || "Could not set workspace.";
      }
    },

    async sendMessage(agentId) {
      const content = (this.drafts[agentId] || "").trim();
      if (!content) return;
      this.drafts[agentId] = "";
      await this.api(`/api/agents/${agentId}/message`, {
        method: "POST",
        body: JSON.stringify({ content, provider: this.provider, model: this.model }),
      });
      await this.refresh(true);
    },

    handleTerminalEnter(agentId, event) {
      if (event.shiftKey) {
        return;
      }
      event.preventDefault();
      this.sendMessage(agentId);
    },

    async injectContext() {
      const content = this.contextDraft.trim();
      if (!content) return;
      this.contextDraft = "";
      await this.api("/api/context", {
        method: "POST",
        body: JSON.stringify({ content }),
      });
      await this.refresh(false);
    },

    async loadRun(runId) {
      const data = await this.api(`/api/runs/${runId}`);
      this.selectedRunId = runId;
      this.traceText = JSON.stringify(data, null, 2);
    },

    transcriptAtBottom(node) {
      return node.scrollHeight - node.scrollTop - node.clientHeight < 28;
    },

    handleTranscriptScroll(agentId, event) {
      this.transcriptPinned[agentId] = !this.transcriptAtBottom(event.currentTarget);
    },

    scrollTranscriptToBottom(agentId) {
      const node = document.querySelector(`.transcript[data-agent-id="${agentId}"]`);
      if (!node) return;
      node.scrollTop = node.scrollHeight;
      this.transcriptPinned[agentId] = false;
    },

    scrollTranscripts(force = false) {
      document.querySelectorAll(".transcript").forEach((node) => {
        const agentId = node.dataset.agentId;
        if (force || !this.transcriptPinned[agentId]) {
          node.scrollTop = node.scrollHeight;
          this.transcriptPinned[agentId] = false;
        }
      });
    },
  };
};
