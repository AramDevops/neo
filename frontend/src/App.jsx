import { useEffect, useMemo, useState } from "react";
import { MetricsBar } from "./components/MetricsBar.jsx";
import { ScreenshotGallery } from "./components/ScreenshotGallery.jsx";
import { SettingsDrawer } from "./components/SettingsDrawer.jsx";
import { TerminalGrid } from "./components/TerminalGrid.jsx";
import { TopBar } from "./components/TopBar.jsx";
import { useBackendState } from "./hooks/useBackendState.js";
import { useProviderEngine } from "./hooks/useProviderEngine.js";
import { useTerminalActions } from "./hooks/useTerminalActions.js";
import { useTranscriptController } from "./hooks/useTranscriptController.js";
import { collectScreenshots } from "./lib/activities.js";
import { api } from "./lib/api.js";
import { readJson } from "./lib/storage.js";
import {
  agentIdList,
  buildTerminalItems
} from "./lib/terminalItems.js";
import {
  dbLabel as formatDbLabel,
  groupTools,
  metricRows as buildMetricRows,
  normalizeAgentText as formatAgentText,
  runAgentLabel as formatRunAgentLabel,
  shortTime as formatShortTime,
  workspaceLabel as formatWorkspaceLabel
} from "./lib/viewModels.js";

const accents = ["#71d88a", "#6fc7d8", "#e0bd64", "#b59cff", "#e07171", "#9cd67a"];

export function App() {
  const [cols, setCols] = useState(2);
  const [rowHeight, setRowHeight] = useState(500);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState("engine");
  const [workspaceDraft, setWorkspaceDraft] = useState("");
  const [workspaceDirty, setWorkspaceDirty] = useState(false);
  const [workspaceError, setWorkspaceError] = useState("");
  const [workspacePicking, setWorkspacePicking] = useState(false);
  const [recentWorkspaces, setRecentWorkspaces] = useState(() => readJson("neo.recentWorkspaces", []));
  const [contextDraft, setContextDraft] = useState("");
  const [traceText, setTraceText] = useState("select a run");
  const [syncActive, setSyncActive] = useState(() => readJson("neo.syncActive", true));
  const [pendingItems, setPendingItems] = useState({});
  const [gallery, setGallery] = useState(null);
  const {
    provider,
    model,
    setModel,
    providerApiKey,
    setProviderApiKey,
    setProviderApiKeyDirty,
    providerApiKeyVisible,
    toggleProviderApiKeyVisible,
    providerBaseUrl,
    setProviderBaseUrl,
    providerModelsText,
    setProviderModelsText,
    providers,
    providerInfo,
    providerNeedsApiKey,
    providerShowsEndpoint,
    modelOptions,
    handleProviderChange,
    hydrateProviderEngine
  } = useProviderEngine();

  const {
    gridRef,
    transcriptRefs,
    messageSigs,
    shouldAnimateMessage,
    finishMessageAnimation,
    transcriptNeedsLatest,
    setTranscriptNeedsLatest,
    containScrollWheel,
    handleTranscriptScroll,
    jumpTranscriptToLatest,
    prepareTyping,
    pruneConfirmedPendingItems,
    syncGeometry,
    syncTranscripts,
    forgetAgents,
    pinAgent
  } = useTranscriptController({ cols, setRowHeight, setPendingItems });
  const {
    agents,
    setAgents,
    messages,
    activities,
    plans,
    runs,
    sharedContext,
    tools,
    metrics,
    config,
    workspace,
    setWorkspaceState,
    refresh,
    pruneBackendAgentState,
    clearBackendAgentState
  } = useBackendState({
    workspaceDirty,
    setWorkspaceDraft,
    syncActive,
    hydrateProviderEngine,
    prepareTyping,
    pruneConfirmedPendingItems,
    syncGeometry,
    syncTranscripts,
    messageSigs,
    forgetAgents,
    setTraceText
  });
  const {
    drafts,
    setDrafts,
    pasteAttachments,
    removePasteAttachment,
    expandedActivities,
    toggleActivity,
    hiddenPlans,
    updateHiddenPlans,
    deletingAgents,
    clearingAgents,
    stoppingAgents,
    deletingAll,
    createAgent,
    clearAgentChat,
    deleteAgent,
    deleteAllAgents,
    stopAgent,
    sendMessage,
    handleTerminalKeyDown,
    handleTerminalPaste,
    handleTerminalDragOver,
    handleTerminalDragLeave,
    handleTerminalDrop,
    attachFiles,
    dropTargets,
    pruneTerminalAgentState
  } = useTerminalActions({
    agents,
    setAgents,
    messages,
    activities,
    provider,
    model,
    refresh,
    pinAgent,
    setPendingItems,
    setTranscriptNeedsLatest,
    setTraceText,
    onDeletedAgents: pruneDeletedAgentState,
    onClearedAgent: clearBackendAgentState
  });

  const visibleAgents = agents;
  const hasRunningAgents = agents.some((agent) => agent.status === "running");
  const agentNameById = useMemo(
    () => new Map(agents.map((agent) => [Number(agent.id), agent.name]).filter(([, name]) => Boolean(name))),
    [agents]
  );
  const aiRetryStatus = useMemo(() => {
    const runningAgentIds = new Set(
      agents.filter((agent) => agent.status === "running").map((agent) => String(agent.id))
    );
    let latest = null;
    Object.entries(activities).forEach(([agentId, items]) => {
      if (!runningAgentIds.has(String(agentId))) return;
      (items || []).forEach((activity) => {
        if (activity.type !== "provider_retry") return;
        if (!latest || Number(activity.id || 0) > Number(latest.id || 0)) latest = activity;
      });
    });
    if (!latest) return null;
    const payload = latest.payload || {};
    return {
      phase: "retry",
      attempt: payload.attempt || 1,
      max: payload.max || 5,
      message: payload.error || payload.message || ""
    };
  }, [activities, agents]);

  function pruneDeletedAgentState(agentIds) {
    const ids = agentIdList(agentIds);
    if (!ids.length) return;
    pruneBackendAgentState(ids);
    pruneTerminalAgentState(ids);
  }

  useEffect(() => {
    localStorage.removeItem("neo.hiddenAgents");
  }, []);

  useEffect(() => {
    const activeIds = new Set(agents.map((agent) => String(agent.id)));
    const knownIds = new Set([
      ...Object.keys(messages),
      ...Object.keys(activities),
      ...Object.keys(plans),
      ...Object.keys(drafts),
      ...Object.keys(pasteAttachments),
      ...Object.keys(pendingItems),
      ...Object.keys(transcriptNeedsLatest),
      ...Object.keys(hiddenPlans),
      ...Object.keys(messageSigs.current)
    ]);
    const staleIds = Array.from(knownIds).filter((id) => !activeIds.has(id));
    if (staleIds.length) pruneDeletedAgentState(staleIds);
  }, [agents, pasteAttachments]);

  useEffect(() => {
    window.requestAnimationFrame(() => {
      syncTranscripts();
    });
    // pendingItems matters: the user's own just-sent message must scroll the
    // transcript immediately, before the server round-trip lands.
  }, [activities, messages, pendingItems, syncTranscripts]);

  function terminalItems(agentId) {
    return buildTerminalItems(agentId, messages, activities, pendingItems);
  }

  function agentToolStats(agentId) {
    const items = activities[agentId] || [];
    const latestRunId = Math.max(
      0,
      ...(messages[agentId] || []).map((message) => Number(message.run_id) || 0),
      ...items.map((activity) => Number(activity.run_id) || 0)
    );
    const toolItems = items.filter((activity) => activity.type === "tool_result");
    const current = latestRunId ? toolItems.filter((activity) => Number(activity.run_id) === latestRunId).length : toolItems.length;
    const total = toolItems.length;
    return {
      current,
      total,
      label: total && current !== total ? `${current}/${total}` : String(current || total || 0)
    };
  }

  function agentScreenshots(agentId) {
    return collectScreenshots(activities[agentId] || []);
  }

  function screenshotCount(agentId) {
    return agentScreenshots(agentId).length;
  }

  function openScreenshot(agentId, activity) {
    const shots = agentScreenshots(agentId);
    const index = shots.findIndex((shot) => String(shot.activityId) === String(activity.id));
    if (!shots.length) return;
    setGallery({ agentId, index: index >= 0 ? index : shots.length - 1 });
  }

  function openGallery(agentId) {
    const shots = agentScreenshots(agentId);
    if (!shots.length) return;
    setGallery({ agentId, index: shots.length - 1 });
  }

  const galleryShots = gallery ? agentScreenshots(gallery.agentId) : [];

  useEffect(() => {
    if (gallery && !galleryShots.length) setGallery(null);
  }, [gallery, galleryShots.length]);

  async function openVscode(path = ".") {
    try {
      await api("/api/workspace/open-vscode", {
        method: "POST",
        body: JSON.stringify({ path })
      });
    } catch (error) {
      setTraceText(error.message || "Could not open VS Code.");
    }
  }

  function planState(agentId) {
    const list = plans[agentId] || [];
    if (!list.length) return "idle";
    if (list.some((plan) => plan.status === "error")) return "blocked";
    if (list.some((plan) => plan.status === "in_progress")) return "in_progress";
    if (list.some((plan) => plan.status === "pending")) return "pending";
    if (list.every((plan) => plan.status === "complete")) return "complete";
    return list[0]?.status || "idle";
  }

  function metricRows() { return buildMetricRows(metrics); }

  function dbLabel() { return formatDbLabel(config); }

  function normalizeAgentText(value) { return formatAgentText(value, agentNameById); }

  function runAgentLabel(run) { return formatRunAgentLabel(run, agentNameById); }

  function workspaceLabel() { return formatWorkspaceLabel(workspace); }

  function shortTime(value) { return formatShortTime(value); }

  function openSettings(tab = "engine") {
    setSettingsTab(tab);
    setSettingsOpen(true);
  }

  function toggleSync() {
    setSyncActive((active) => !active);
  }

  function rememberWorkspace(path) {
    const clean = String(path || "").trim();
    if (!clean) return;
    localStorage.setItem("neo.lastWorkspace", clean);
    setRecentWorkspaces((prev) => {
      const next = [clean, ...prev.filter((item) => item !== clean)].slice(0, 8);
      localStorage.setItem("neo.recentWorkspaces", JSON.stringify(next));
      return next;
    });
  }

  function fillWorkspace(path) {
    setWorkspaceDraft(path);
    setWorkspaceDirty(true);
    setWorkspaceError("");
  }

  async function injectContext() {
    const content = contextDraft.trim();
    if (!content) return;
    setContextDraft("");
    await api("/api/context", {
      method: "POST",
      body: JSON.stringify({ content })
    });
    await refresh(false);
  }

  async function loadRun(runId) {
    const data = await api(`/api/runs/${runId}`);
    setTraceText(JSON.stringify(data, null, 2));
  }

  async function setWorkspace() {
    const path = workspaceDraft.trim();
    if (!path) {
      setWorkspaceError("Workspace path is required.");
      return;
    }
    setWorkspaceError("");
    try {
      const next = await api("/api/workspace", {
        method: "POST",
        body: JSON.stringify({ path, create: true })
      });
      setWorkspaceState(next);
      setWorkspaceDraft(next.workspace_dir || path);
      setWorkspaceDirty(false);
      rememberWorkspace(next.workspace_dir || path);
      await refresh(false);
    } catch (error) {
      setWorkspaceError(error.message || "Could not set workspace.");
    }
  }

  async function pickWorkspace() {
    if (workspacePicking) return;
    setWorkspacePicking(true);
    setWorkspaceError("");
    try {
      const data = await api("/api/workspace/pick", {
        method: "POST",
        body: JSON.stringify({ initial: workspaceDraft || workspace.workspace_dir || localStorage.getItem("neo.lastWorkspace") || "" })
      });
      if (data.cancelled) return;
      const next = data.workspace || {};
      setWorkspaceState(next);
      setWorkspaceDraft(next.workspace_dir || "");
      setWorkspaceDirty(false);
      rememberWorkspace(next.workspace_dir || "");
      await refresh(false);
    } catch (error) {
      setWorkspaceError(error.message || "Could not open folder picker.");
    } finally {
      setWorkspacePicking(false);
    }
  }

  function toolGroups() {
    return groupTools(tools);
  }

  return (
    <div className="shell">
      <TopBar
        workspace={workspace}
        workspaceLabel={workspaceLabel}
        openSettings={openSettings}
        createAgent={createAgent}
        deleteAllAgents={deleteAllAgents}
        deletingAll={deletingAll}
        hasRunningAgents={hasRunningAgents}
        hasAgents={Boolean(agents.length)}
        syncActive={syncActive}
        toggleSync={toggleSync}
        aiRetryStatus={aiRetryStatus}
      />

      <MetricsBar metrics={metricRows()} />

      <main className="workbench">
        <TerminalGrid
          gridRef={gridRef}
          visibleAgents={visibleAgents}
          cols={cols}
          rowHeight={rowHeight}
          accents={accents}
          hiddenPlans={hiddenPlans}
          updateHiddenPlans={updateHiddenPlans}
          clearAgentChat={clearAgentChat}
          clearingAgents={clearingAgents}
          deleteAgent={deleteAgent}
          deletingAgents={deletingAgents}
          stopAgent={stopAgent}
          stoppingAgents={stoppingAgents}
          terminalItems={terminalItems}
          toolStats={agentToolStats}
          normalizeAgentText={normalizeAgentText}
          shouldAnimateMessage={shouldAnimateMessage}
          finishMessageAnimation={finishMessageAnimation}
          syncTranscripts={syncTranscripts}
          transcriptRefs={transcriptRefs}
          handleTranscriptScroll={handleTranscriptScroll}
          containScrollWheel={containScrollWheel}
          transcriptNeedsLatest={transcriptNeedsLatest}
          jumpTranscriptToLatest={jumpTranscriptToLatest}
          plans={plans}
          planState={planState}
          pasteAttachments={pasteAttachments}
          removePasteAttachment={removePasteAttachment}
          drafts={drafts}
          setDrafts={setDrafts}
          handleTerminalKeyDown={handleTerminalKeyDown}
          handleTerminalPaste={handleTerminalPaste}
          handleTerminalDragOver={handleTerminalDragOver}
          handleTerminalDragLeave={handleTerminalDragLeave}
          handleTerminalDrop={handleTerminalDrop}
          attachFiles={attachFiles}
          dropTargets={dropTargets}
          sendMessage={sendMessage}
          expandedActivities={expandedActivities}
          toggleActivity={toggleActivity}
          openVscode={openVscode}
          screenshotCount={screenshotCount}
          openGallery={openGallery}
          openScreenshot={openScreenshot}
        />

        {settingsOpen && (
          <SettingsDrawer
            containScrollWheel={containScrollWheel}
            provider={provider}
            model={model}
            setSettingsOpen={setSettingsOpen}
            settingsTab={settingsTab}
            setSettingsTab={setSettingsTab}
            providers={providers}
            handleProviderChange={handleProviderChange}
            modelOptions={modelOptions}
            setModel={setModel}
            providerNeedsApiKey={providerNeedsApiKey}
            providerApiKey={providerApiKey}
            setProviderApiKey={setProviderApiKey}
            setProviderApiKeyDirty={setProviderApiKeyDirty}
            providerApiKeyVisible={providerApiKeyVisible}
            toggleProviderApiKeyVisible={toggleProviderApiKeyVisible}
            providerShowsEndpoint={providerShowsEndpoint}
            providerBaseUrl={providerBaseUrl}
            setProviderBaseUrl={setProviderBaseUrl}
            providerModelsText={providerModelsText}
            setProviderModelsText={setProviderModelsText}
            cols={cols}
            setCols={setCols}
            providerInfo={providerInfo}
            workspace={workspace}
            workspaceDraft={workspaceDraft}
            setWorkspaceDraft={setWorkspaceDraft}
            setWorkspaceDirty={setWorkspaceDirty}
            setWorkspaceError={setWorkspaceError}
            pickWorkspace={pickWorkspace}
            workspacePicking={workspacePicking}
            setWorkspace={setWorkspace}
            recentWorkspaces={recentWorkspaces}
            fillWorkspace={fillWorkspace}
            workspaceError={workspaceError}
            contextDraft={contextDraft}
            setContextDraft={setContextDraft}
            injectContext={injectContext}
            sharedContext={sharedContext}
            normalizeAgentText={normalizeAgentText}
            shortTime={shortTime}
            runs={runs}
            loadRun={loadRun}
            runAgentLabel={runAgentLabel}
            traceText={traceText}
            toolGroups={toolGroups}
            dbLabel={dbLabel}
          />
        )}
      </main>

      {gallery && galleryShots.length > 0 && (
        <ScreenshotGallery
          agent={agents.find((agent) => String(agent.id) === String(gallery.agentId))}
          shots={galleryShots}
          index={gallery.index}
          onSelect={(index) => setGallery((prev) => (prev ? { ...prev, index } : prev))}
          onClose={() => setGallery(null)}
        />
      )}
    </div>
  );
}
