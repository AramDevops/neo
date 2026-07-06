import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api.js";
import { agentIdList, removeAgentKeys } from "../lib/terminalItems.js";

// Poll cadence and a MODULE-LEVEL rate floor. The per-effect in-flight guard
// only coordinates one interval; dev HMR re-execution and repeated remounts
// can leak several intervals, each with its own guard, and together they
// hammered /api/state + /api/agents/* dozens of times a second. This shared
// gate caps the real poll rate no matter how many intervals exist: every
// ticker checks the same timestamp and flag, so at most one refresh runs per
// POLL_INTERVAL_MS process-wide.
const POLL_INTERVAL_MS = 4000;
const pollGate = { inFlight: false, lastStart: 0 };

export function useBackendState({
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
}) {
  const [agents, setAgents] = useState([]);
  const [messages, setMessages] = useState({});
  const [activities, setActivities] = useState({});
  const [plans, setPlans] = useState({});
  const [runs, setRuns] = useState([]);
  const [sharedContext, setSharedContext] = useState([]);
  const [tools, setTools] = useState([]);
  const [metrics, setMetrics] = useState({});
  const [config, setConfig] = useState({});
  const [workspace, setWorkspaceState] = useState({});
  const messagesRef = useRef({});
  const stateSigs = useRef({});
  const agentDataSigs = useRef({});

  // Polling must not force renders when nothing changed: replacing every
  // state object each tick re-rendered the whole workbench every 4s and,
  // combined with heavy transcripts, contributed to UI freezes.
  const setIfChanged = useCallback((key, value, setter) => {
    let sig;
    try {
      sig = JSON.stringify(value);
    } catch {
      sig = undefined;
    }
    if (sig !== undefined && stateSigs.current[key] === sig) return;
    stateSigs.current[key] = sig;
    setter(value);
  }, []);

  const loadState = useCallback(async () => {
    const data = await api("/api/state");
    setIfChanged("agents", data.agents || [], setAgents);
    setIfChanged("runs", data.runs || [], setRuns);
    setIfChanged("sharedContext", data.shared_context || [], setSharedContext);
    // metrics.now is a server timestamp that changes every poll; keeping it
    // would defeat the change detection (and nothing renders it).
    const { now: _serverNow, ...stableMetrics } = data.metrics || {};
    setIfChanged("metrics", stableMetrics, setMetrics);
    setIfChanged("config", data.config || {}, setConfig);
    setIfChanged("tools", data.tools || [], setTools);
    const nextCatalog = data.model_catalog || { providers: [] };
    setIfChanged("workspace", data.workspace || {}, setWorkspaceState);
    if (!workspaceDirty) {
      setWorkspaceDraft(data.workspace?.workspace_dir || localStorage.getItem("neo.lastWorkspace") || "");
    }
    hydrateProviderEngine(nextCatalog, data.config || {});
    return data.agents || [];
  }, [hydrateProviderEngine, setIfChanged, setWorkspaceDraft, workspaceDirty]);

  const loadMessages = useCallback(async (agentList, force = false) => {
    await Promise.all(agentList.map(async (agent) => {
      const sig = [agent.id, agent.status, agent.updated_at || "", agent.provider, agent.model].join("|");
      const currentMessages = messagesRef.current;
      const shouldLoad = force || !currentMessages[agent.id] || agent.status === "running" || messageSigs.current[agent.id] !== sig;
      if (!shouldLoad) return;
      const firstLoad = !currentMessages[agent.id];
      const data = await api(`/api/agents/${agent.id}/messages`);
      const nextMessages = data.messages || [];
      const nextActivities = data.activities || [];
      // Running agents are re-fetched every tick; only touch state when the
      // payload actually advanced (new rows or plan status changes).
      const dataSig = JSON.stringify([
        nextMessages.length,
        nextMessages.length ? nextMessages[nextMessages.length - 1].id : 0,
        nextActivities.length,
        nextActivities.length ? nextActivities[nextActivities.length - 1].id : 0,
        (data.plans || []).map((plan) => [plan.id, plan.status]),
      ]);
      if (!force && !firstLoad && agentDataSigs.current[agent.id] === dataSig) {
        messageSigs.current[agent.id] = sig;
        return;
      }
      agentDataSigs.current[agent.id] = dataSig;
      prepareTyping(nextMessages, firstLoad);
      pruneConfirmedPendingItems(agent.id, nextMessages, nextActivities);
      setMessages((prev) => {
        const next = { ...prev, [agent.id]: nextMessages };
        messagesRef.current = next;
        return next;
      });
      setActivities((prev) => ({ ...prev, [agent.id]: nextActivities }));
      setPlans((prev) => ({ ...prev, [agent.id]: data.plans || [] }));
      messageSigs.current[agent.id] = sig;
    }));
  }, [messageSigs, prepareTyping, pruneConfirmedPendingItems]);

  const refresh = useCallback(async (forceMessages = false) => {
    const agentList = await loadState();
    await loadMessages(agentList, forceMessages);
    window.requestAnimationFrame(() => {
      syncGeometry();
      syncTranscripts();
    });
  }, [loadMessages, loadState, syncGeometry, syncTranscripts]);

  const pruneBackendAgentState = useCallback((agentIds) => {
    const ids = agentIdList(agentIds);
    if (!ids.length) return;
    setMessages((prev) => {
      const next = removeAgentKeys(prev, ids);
      messagesRef.current = next;
      return next;
    });
    setActivities((prev) => removeAgentKeys(prev, ids));
    setPlans((prev) => removeAgentKeys(prev, ids));
    ids.forEach((id) => {
      delete agentDataSigs.current[id];
    });
    forgetAgents(ids);
  }, [forgetAgents]);

  const clearBackendAgentState = useCallback((agentIds) => {
    const ids = agentIdList(agentIds);
    if (!ids.length) return;
    setMessages((prev) => {
      const next = { ...prev };
      ids.forEach((id) => { next[id] = []; });
      messagesRef.current = next;
      return next;
    });
    setActivities((prev) => {
      const next = { ...prev };
      ids.forEach((id) => { next[id] = []; });
      return next;
    });
    setPlans((prev) => {
      const next = { ...prev };
      ids.forEach((id) => { next[id] = []; });
      return next;
    });
    ids.forEach((id) => {
      delete messageSigs.current[id];
      delete agentDataSigs.current[id];
    });
  }, [messageSigs]);

  useEffect(() => {
    let active = true;
    const boot = async () => {
      const agentList = await loadState();
      if (!active) return;
      await loadMessages(agentList, true);
      window.requestAnimationFrame(() => {
        syncGeometry();
        syncTranscripts();
      });
    };
    boot().catch((error) => setTraceText(error.message));
    return () => {
      active = false;
    };
  }, [loadMessages, loadState, setTraceText, syncGeometry, syncTranscripts]);

  const refreshRef = useRef(refresh);
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);

  useEffect(() => {
    localStorage.setItem("neo.syncActive", JSON.stringify(syncActive));
    if (!syncActive) return undefined;
    // Poll through a ref with a MODULE-LEVEL gate (pollGate): the shared flag
    // and timestamp mean any number of leaked/duplicate intervals still emit
    // at most one refresh per POLL_INTERVAL_MS, and never overlap. Depending on
    // refresh() identity used to re-arm the interval on every render, which
    // compounded into a request storm that froze the UI.
    const tick = async () => {
      const now = Date.now();
      if (pollGate.inFlight) return;
      if (now - pollGate.lastStart < POLL_INTERVAL_MS - 250) return;
      pollGate.inFlight = true;
      pollGate.lastStart = now;
      try {
        await refreshRef.current(false);
      } catch {
        // next tick retries; the api layer already reports failures
      } finally {
        pollGate.inFlight = false;
      }
    };
    tick();
    const interval = window.setInterval(tick, POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [syncActive]);

  return {
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
  };
}
