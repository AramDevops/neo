import { useEffect, useRef, useState } from "react";
import { pastedBlock, pasteTitle } from "../components/MessageText.jsx";
import { api, apiUpload } from "../lib/api.js";
import { slashCommandMatches } from "../lib/slashCommands.js";
import {
  LONG_PASTE_CHARS,
  LONG_PASTE_LINES,
  PENDING_SEQUENCE,
  agentIdList,
  itemPendingData,
  itemPendingToken,
  numericValue,
  removeAgentKeys
} from "../lib/terminalItems.js";
import { readJson } from "../lib/storage.js";

function deletedIdsFromSummary(summary, fallback) {
  const ids = agentIdList(summary?.deleted_agents);
  return ids.length ? ids : agentIdList(fallback);
}

function deleteErrorText(error, fallback = "Could not delete terminal.") {
  if (error?.data?.running?.length) {
    const names = error.data.running.map((agent) => agent.name || `#${agent.id}`).join(", ");
    return `Cannot delete while running: ${names}`;
  }
  if (error?.data?.agent?.name) return `Cannot delete while running: ${error.data.agent.name}`;
  return error?.message || fallback;
}

function clearErrorText(error) {
  if (error?.data?.agent?.name) return `Cannot clear while running: ${error.data.agent.name}`;
  return error?.message || "Could not clear chat.";
}

export function useTerminalActions({
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
  onDeletedAgents,
  onClearedAgent
}) {
  const [drafts, setDrafts] = useState({});
  const [pasteAttachments, setPasteAttachments] = useState({});
  const [dropTargets, setDropTargets] = useState({});
  const [expandedActivities, setExpandedActivities] = useState({});
  const [hiddenPlans, setHiddenPlans] = useState(() => readJson("neo.hiddenPlans", {}));
  const [deletingAgents, setDeletingAgents] = useState({});
  const [clearingAgents, setClearingAgents] = useState({});
  const [stoppingAgents, setStoppingAgents] = useState({});
  const [deletingAll, setDeletingAll] = useState(false);
  // Tab-completion cycle for slash commands: valid only while the draft still
  // equals the completion Tab last inserted; any typing invalidates it.
  const tabCycle = useRef({ agentId: null, completions: [], index: -1 });

  // A stop is cooperative: the backend ends the run at its next checkpoint.
  // "stopping" holds until the agent actually leaves the running status, so
  // the button reads honestly and a stale flag can never mark a later run.
  useEffect(() => {
    setStoppingAgents((prev) => {
      const entries = Object.entries(prev).filter(([agentId]) =>
        agents.some((agent) => String(agent.id) === String(agentId) && agent.status === "running"));
      if (entries.length === Object.keys(prev).length) return prev;
      return Object.fromEntries(entries);
    });
  }, [agents]);

  function pruneTerminalAgentState(agentIds) {
    const ids = agentIdList(agentIds);
    if (!ids.length) return;
    setDrafts((prev) => removeAgentKeys(prev, ids));
    setPasteAttachments((prev) => removeAgentKeys(prev, ids));
    setDropTargets((prev) => removeAgentKeys(prev, ids));
    setPendingItems((prev) => removeAgentKeys(prev, ids));
    setTranscriptNeedsLatest((prev) => removeAgentKeys(prev, ids));
    setDeletingAgents((prev) => removeAgentKeys(prev, ids));
    setClearingAgents((prev) => removeAgentKeys(prev, ids));
    setStoppingAgents((prev) => removeAgentKeys(prev, ids));
    setHiddenPlans((prev) => {
      const next = removeAgentKeys(prev, ids);
      if (next !== prev) localStorage.setItem("neo.hiddenPlans", JSON.stringify(next));
      return next;
    });
  }

  function toggleActivity(key) {
    setExpandedActivities((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  function updateHiddenPlans(agentId) {
    setHiddenPlans((prev) => {
      const next = { ...prev, [agentId]: !prev[agentId] };
      localStorage.setItem("neo.hiddenPlans", JSON.stringify(next));
      return next;
    });
  }

  function latestMessageId(agentId) {
    return (messages[agentId] || []).reduce((max, message) => Math.max(max, numericValue(message.id)), 0);
  }

  function latestRunId(agentId) {
    const messageRunId = (messages[agentId] || []).reduce((max, message) => Math.max(max, numericValue(message.run_id)), 0);
    const activityRunId = (activities[agentId] || []).reduce((max, activity) => Math.max(max, numericValue(activity.run_id)), 0);
    return Math.max(messageRunId, activityRunId);
  }

  function isAgentRunning(agentId) {
    return agents.find((agent) => agent.id === agentId)?.status === "running";
  }

  function attachmentBlock(item) {
    if (item.kind === "file") {
      return `[Neo attached file: ${item.relativePath} | ${item.size} bytes]`;
    }
    return pastedBlock(item);
  }

  function composeMessageContent(agentId) {
    const body = (drafts[agentId] || "").trim();
    const attachments = pasteAttachments[agentId] || [];
    const blocks = attachments.map(attachmentBlock);
    return [body, ...blocks].filter(Boolean).join("\n\n").trim();
  }

  function addPasteAttachment(agentId, text) {
    const content = String(text || "");
    if (!content.trim()) return;
    const item = {
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      title: pasteTitle(content),
      content,
      chars: content.length
    };
    setPasteAttachments((prev) => ({ ...prev, [agentId]: [...(prev[agentId] || []), item] }));
  }

  function removePasteAttachment(agentId, attachmentId) {
    setPasteAttachments((prev) => {
      const nextItems = (prev[agentId] || []).filter((item) => item.id !== attachmentId);
      const next = { ...prev };
      if (nextItems.length) next[agentId] = nextItems;
      else delete next[agentId];
      return next;
    });
  }

  function handleTerminalPaste(event, agentId) {
    const files = Array.from(event.clipboardData?.files || []);
    if (files.length) {
      event.preventDefault();
      attachDroppedFiles(agentId, files);
      return;
    }
    const text = event.clipboardData?.getData("text") || "";
    const lineCount = text.split(/\r?\n/).length;
    if (text.length < LONG_PASTE_CHARS && lineCount < LONG_PASTE_LINES) return;
    event.preventDefault();
    addPasteAttachment(agentId, text);
  }

  function setDropTarget(agentId, active) {
    setDropTargets((prev) => {
      if (Boolean(prev[agentId]) === active) return prev;
      const next = { ...prev };
      if (active) next[agentId] = true;
      else delete next[agentId];
      return next;
    });
  }

  function handleTerminalDragOver(event, agentId) {
    if (!Array.from(event.dataTransfer?.types || []).includes("Files")) return;
    event.preventDefault();
    setDropTarget(agentId, true);
  }

  function handleTerminalDragLeave(event, agentId) {
    if (event.currentTarget.contains(event.relatedTarget)) return;
    setDropTarget(agentId, false);
  }

  async function handleTerminalDrop(event, agentId) {
    const files = Array.from(event.dataTransfer?.files || []);
    if (!files.length) return;
    event.preventDefault();
    setDropTarget(agentId, false);
    await attachDroppedFiles(agentId, files);
  }

  async function attachDroppedFiles(agentId, files) {
    const batch = files.slice(0, 8);
    const formData = new FormData();
    batch.forEach((file) => formData.append("files", file, file.name));
    try {
      const result = await apiUpload("/api/uploads", formData);
      const uploaded = result.files || [];
      if (!uploaded.length) return;
      setPasteAttachments((prev) => ({
        ...prev,
        [agentId]: [
          ...(prev[agentId] || []),
          ...uploaded.map((file) => ({
            id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
            kind: "file",
            title: file.name,
            relativePath: file.relative_path,
            size: Number(file.size) || 0
          }))
        ]
      }));
    } catch (error) {
      setTraceText(error.message || "Could not upload the dropped files.");
    }
  }

  async function createAgent() {
    await api("/api/agents", {
      method: "POST",
      body: JSON.stringify({ provider, model })
    });
    await refresh(true);
  }

  async function deleteAgent(agentId) {
    if (deletingAgents[agentId]) return;
    setDeletingAgents((prev) => ({ ...prev, [agentId]: true }));
    try {
      const summary = await api(`/api/agents/${agentId}`, { method: "DELETE" });
      onDeletedAgents(deletedIdsFromSummary(summary, agentId));
      await refresh(true);
    } catch (error) {
      setTraceText(deleteErrorText(error));
    } finally {
      setDeletingAgents((prev) => removeAgentKeys(prev, [agentId]));
    }
  }

  function applyClearedTerminalState(agentId) {
    const ids = [agentId];
    setDrafts((prev) => removeAgentKeys(prev, ids));
    setPasteAttachments((prev) => removeAgentKeys(prev, ids));
    setPendingItems((prev) => removeAgentKeys(prev, ids));
    setTranscriptNeedsLatest((prev) => removeAgentKeys(prev, ids));
    setAgents((prev) => prev.map((agent) => agent.id === agentId ? { ...agent, status: "idle" } : agent));
    onClearedAgent(agentId);
  }

  async function clearAgentChat(agentId) {
    if (clearingAgents[agentId]) return;
    setClearingAgents((prev) => ({ ...prev, [agentId]: true }));
    try {
      await api(`/api/agents/${agentId}/clear`, { method: "POST" });
      applyClearedTerminalState(agentId);
      await refresh(true);
    } catch (error) {
      setTraceText(clearErrorText(error));
    } finally {
      setClearingAgents((prev) => removeAgentKeys(prev, [agentId]));
    }
  }

  async function stopAgent(agentId) {
    if (stoppingAgents[agentId] || !isAgentRunning(agentId)) return;
    setStoppingAgents((prev) => ({ ...prev, [agentId]: true }));
    try {
      const response = await api(`/api/agents/${agentId}/stop`, { method: "POST" });
      if (response.status === "reset") {
        // No live run existed behind the running status; backend reset it.
        setAgents((prev) => prev.map((agent) => agent.id === agentId ? { ...agent, status: "idle" } : agent));
      }
      await refresh(true);
    } catch (error) {
      setStoppingAgents((prev) => removeAgentKeys(prev, [agentId]));
      if (error?.status === 409) {
        // The run finished on its own before the stop landed.
        await refresh(true);
        return;
      }
      setTraceText(error?.message || "Could not stop the run.");
    }
  }

  async function deleteAllAgents() {
    if (!agents.length || deletingAll) return;
    setDeletingAll(true);
    try {
      const summary = await api("/api/agents", { method: "DELETE" });
      onDeletedAgents(deletedIdsFromSummary(summary, agents.map((agent) => agent.id)));
      await refresh(true);
    } catch (error) {
      setTraceText(deleteErrorText(error, "Could not delete terminals."));
    } finally {
      setDeletingAll(false);
    }
  }

  async function sendMessage(agentId) {
    const content = composeMessageContent(agentId);
    if (!content) return;
    const now = new Date().toISOString();
    const token = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const injecting = isAgentRunning(agentId);
    const baseRunId = latestRunId(agentId);
    const pendingMeta = {
      token,
      content,
      baseMessageId: latestMessageId(agentId),
      sentAt: now,
      runGroup: injecting && baseRunId ? baseRunId : baseRunId + 0.5
    };
    const pendingRows = [
      {
        kind: "message",
        key: `pending-message-${token}`,
        created_at: now,
        pending: { ...pendingMeta, phase: injecting ? 10 : 0, sequence: injecting ? PENDING_SEQUENCE - 1 : 0 },
        message: {
          id: `pending-message-${token}`,
          role: "user",
          content,
          created_at: now,
          pendingToken: token,
          pending: pendingMeta
        }
      },
      {
        kind: "activity",
        key: `pending-activity-${token}`,
        created_at: now,
        pending: { ...pendingMeta, phase: 10, sequence: injecting ? PENDING_SEQUENCE : 1 },
        activity: {
          id: `pending-activity-${token}`,
          type: "ui_pending",
          run_id: "live",
          created_at: now,
          pendingToken: token,
          pending: pendingMeta,
          payload: { status: "thinking", detail: "Queued locally. Waiting for engine/tool output." }
        }
      }
    ];
    setDrafts((prev) => ({ ...prev, [agentId]: "" }));
    setPasteAttachments((prev) => {
      const next = { ...prev };
      delete next[agentId];
      return next;
    });
    setPendingItems((prev) => ({ ...prev, [agentId]: [...(prev[agentId] || []), ...pendingRows] }));
    setAgents((prev) => prev.map((agent) => agent.id === agentId ? { ...agent, status: "running" } : agent));
    pinAgent(agentId);
    try {
      const response = await api(`/api/agents/${agentId}/message`, {
        method: "POST",
        body: JSON.stringify({ content, provider, model })
      });
      if (response.message_id) {
        setPendingItems((prev) => ({
          ...prev,
          [agentId]: (prev[agentId] || []).map((item) => {
            if (itemPendingToken(item) !== token) return item;
            const pending = { ...itemPendingData(item), serverMessageId: response.message_id };
            return {
              ...item,
              pending,
              message: item.message ? { ...item.message, pending } : item.message,
              activity: item.activity ? { ...item.activity, pending } : item.activity
            };
          })
        }));
      }
      if (response.status === "handled") {
        // Slash command: answered instantly by the backend, no run started.
        if (response.cleared) {
          // /clear wiped the backend history; drop every local trace too.
          applyClearedTerminalState(agentId);
        } else {
          // Drop the optimistic pending rows and restore idle status.
          setPendingItems((prev) => ({
            ...prev,
            [agentId]: (prev[agentId] || []).filter((item) => !item.key.includes(token))
          }));
          setAgents((prev) => prev.map((agent) => agent.id === agentId ? { ...agent, status: "idle" } : agent));
        }
      }
      if (response.status === "injected") {
        setPendingItems((prev) => ({
          ...prev,
          [agentId]: (prev[agentId] || []).map((item) => item.key === `pending-activity-${token}`
            ? { ...item, activity: { ...item.activity, payload: { status: "context added", detail: "Added to the active run context." } } }
            : item)
        }));
      }
      await refresh(true);
    } catch (error) {
      setTraceText(error.message || "Could not send message.");
      setAgents((prev) => prev.map((agent) => agent.id === agentId ? { ...agent, status: "idle" } : agent));
      setPendingItems((prev) => ({
        ...prev,
        [agentId]: (prev[agentId] || []).filter((item) => !item.key.includes(token))
      }));
    } finally {
      window.setTimeout(() => refresh(false), 250);
    }
  }

  function completeSlashCommand(event, agentId) {
    const draft = drafts[agentId] || "";
    if (!draft.startsWith("/") || draft.includes("\n")) return false;
    event.preventDefault();
    const cycle = tabCycle.current;
    const continuing = cycle.agentId === agentId && cycle.index >= 0 && cycle.completions[cycle.index] === draft;
    if (continuing) {
      const step = event.shiftKey ? -1 : 1;
      cycle.index = (cycle.index + step + cycle.completions.length) % cycle.completions.length;
      setDrafts((prev) => ({ ...prev, [agentId]: cycle.completions[cycle.index] }));
      return true;
    }
    const matches = slashCommandMatches(draft);
    tabCycle.current = { agentId: null, completions: [], index: -1 };
    if (!matches.length) return true;
    if (matches.length === 1) {
      setDrafts((prev) => ({ ...prev, [agentId]: `${matches[0].cmd} ` }));
      return true;
    }
    const index = event.shiftKey ? matches.length - 1 : 0;
    tabCycle.current = { agentId, completions: matches.map((item) => item.cmd), index };
    setDrafts((prev) => ({ ...prev, [agentId]: matches[index].cmd }));
    return true;
  }

  function handleTerminalKeyDown(event, agentId) {
    if (event.key === "Tab") {
      completeSlashCommand(event, agentId);
      return;
    }
    if (event.key !== "Enter") return;
    if (event.shiftKey) return;
    event.preventDefault();
    sendMessage(agentId);
  }

  return {
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
    attachFiles: attachDroppedFiles,
    dropTargets,
    pruneTerminalAgentState
  };
}
