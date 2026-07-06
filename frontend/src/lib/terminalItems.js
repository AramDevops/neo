export const NO_RUN_GROUP = 9000000000000;
export const PENDING_SEQUENCE = 8000000000000;
export const LONG_PASTE_CHARS = 240;
export const LONG_PASTE_LINES = 4;
// Hard cap on rendered transcript items per terminal. Bounds DOM size and
// per-poll render cost regardless of how long a session runs.
export const MAX_TERMINAL_ITEMS = 220;

export function messageKey(message) {
  return String(message.id || `${message.role}-${message.created_at || ""}-${message.content?.length || 0}`);
}

export function numericValue(value, fallback = 0) {
  const next = Number(value);
  return Number.isFinite(next) ? next : fallback;
}

export function timeValue(value) {
  const next = Date.parse(value || "");
  return Number.isFinite(next) ? next : 0;
}

export function contentSignature(value) {
  return String(value || "").replace(/\r\n/g, "\n").trim();
}

export function pendingTokenFromKey(key) {
  const match = String(key || "").match(/^pending-(?:message|activity)-(.+)$/);
  return match ? match[1] : "";
}

export function itemPendingToken(item) {
  return item.pending?.token || item.message?.pendingToken || item.activity?.pendingToken || pendingTokenFromKey(item.key);
}

export function itemPendingData(item) {
  return item.pending || item.message?.pending || item.activity?.pending || {};
}

export function confirmedPendingMatches(items, backendMessages) {
  const groups = new Map();
  items.forEach((item, index) => {
    const token = itemPendingToken(item);
    if (!token || groups.has(token)) return;
    const pending = itemPendingData(item);
    const content = contentSignature(pending.content ?? item.message?.content ?? "");
    if (!content) return;
    groups.set(token, {
      token,
      content,
      baseMessageId: numericValue(pending.baseMessageId),
      serverMessageId: numericValue(pending.serverMessageId || pending.messageId),
      sentAt: timeValue(pending.sentAt),
      index
    });
  });
  if (!groups.size) return new Map();

  const candidates = (backendMessages || [])
    .filter((message) => message.role === "user")
    .map((message, index) => ({
      index,
      message,
      id: numericValue(message.id),
      runId: numericValue(message.run_id),
      content: contentSignature(message.content),
      createdAt: timeValue(message.created_at)
    }))
    .sort((a, b) => a.id - b.id || a.createdAt - b.createdAt || a.index - b.index);

  const claimed = new Set();
  const confirmed = new Map();
  Array.from(groups.values())
    .sort((a, b) => a.sentAt - b.sentAt || a.index - b.index)
    .forEach((group) => {
      const match = candidates.find((candidate) => {
        if (claimed.has(candidate.index)) return false;
        if (group.serverMessageId && candidate.id === group.serverMessageId) return true;
        if (candidate.content !== group.content) return false;
        if (candidate.id > group.baseMessageId) return true;
        return Boolean(group.sentAt && candidate.createdAt >= group.sentAt - 5000);
      });
      if (!match) return;
      claimed.add(match.index);
      confirmed.set(group.token, match.message);
    });
  return confirmed;
}

export function confirmedPendingTokens(items, backendMessages) {
  return new Set(confirmedPendingMatches(items, backendMessages).keys());
}

function realActivityAfter(activity, sentAt) {
  if (!sentAt) return true;
  return timeValue(activity.created_at) >= sentAt - 5000;
}

export function filterConfirmedPendingItems(items, backendMessages, backendActivities = []) {
  const confirmed = confirmedPendingMatches(items, backendMessages);
  if (!confirmed.size && !backendActivities.length) return items;
  return items.filter((item) => {
    const token = itemPendingToken(item);
    if (item.kind === "message") return !confirmed.has(token);
    if (item.kind !== "activity") return true;
    const pending = itemPendingData(item);
    const sentAt = timeValue(pending.sentAt || item.created_at);
    const matchedMessage = confirmed.get(token);
    const matchedRunId = numericValue(matchedMessage?.run_id);
    return !backendActivities.some((activity) => {
      if (matchedRunId && numericValue(activity.run_id) === matchedRunId) return true;
      return realActivityAfter(activity, sentAt);
    });
  });
}

export function agentIdList(value) {
  return (Array.isArray(value) ? value : [value])
    .map((item) => Number(item))
    .filter((item) => Number.isFinite(item) && item > 0);
}

export function removeAgentKeys(record, agentIds) {
  const keys = new Set(agentIdList(agentIds).map(String));
  if (!keys.size) return record;
  let changed = false;
  const next = { ...record };
  keys.forEach((id) => {
    if (Object.prototype.hasOwnProperty.call(next, id)) {
      delete next[id];
      changed = true;
    }
  });
  return changed ? next : record;
}

export function wheelCanScroll(node, deltaY) {
  if (!node || node.scrollHeight <= node.clientHeight + 1 || deltaY === 0) return false;
  if (deltaY < 0) return node.scrollTop > 0;
  return node.scrollTop + node.clientHeight < node.scrollHeight - 1;
}

export function noRunGroup(createdAt) {
  return NO_RUN_GROUP + Math.floor((timeValue(createdAt) || 0) / 1000);
}

export function messageSort(message, index, contextActivity) {
  const messageId = numericValue(message.id, index);
  const contextEventId = numericValue(contextActivity?.id);
  const runId = numericValue(message.run_id) || numericValue(contextActivity?.run_id);
  const role = message.role || "";
  const time = timeValue(message.created_at);
  if (runId) {
    if (contextActivity && role === "user") {
      return { runGroup: runId, phase: 10, sequence: contextEventId - 0.25, time, index };
    }
    return {
      runGroup: runId,
      phase: role === "user" ? 0 : role === "assistant" ? 90 : 50,
      sequence: messageId || index,
      time,
      index
    };
  }
  return {
    runGroup: noRunGroup(message.created_at),
    phase: role === "assistant" ? 90 : 0,
    sequence: messageId || index,
    time,
    index
  };
}

export function activitySort(activity, index) {
  return {
    runGroup: numericValue(activity.run_id) || noRunGroup(activity.created_at),
    phase: 10,
    sequence: numericValue(activity.id, index),
    time: timeValue(activity.created_at),
    index
  };
}

export function pendingSort(item, index) {
  const pending = itemPendingData(item);
  return {
    runGroup: numericValue(pending.runGroup, noRunGroup(item.created_at)),
    phase: numericValue(pending.phase, item.kind === "activity" ? 10 : 0),
    sequence: numericValue(pending.sequence, PENDING_SEQUENCE + index),
    time: timeValue(pending.sentAt || item.created_at),
    index
  };
}

export function pendingSortWithMatch(item, index, matchedMessage) {
  const matchedRunId = numericValue(matchedMessage?.run_id);
  if (!matchedRunId) return pendingSort(item, index);
  const matchedMessageId = numericValue(matchedMessage?.id);
  const pending = itemPendingData(item);
  return {
    runGroup: matchedRunId,
    phase: item.kind === "activity" ? 10 : 0,
    sequence: item.kind === "activity" ? matchedMessageId + 0.25 : matchedMessageId || numericValue(pending.sequence, index),
    time: timeValue(pending.sentAt || item.created_at),
    index
  };
}

export function compareTerminalItems(a, b) {
  const sortA = a.sort || pendingSort(a, 0);
  const sortB = b.sort || pendingSort(b, 0);
  if (sortA.runGroup !== sortB.runGroup) return sortA.runGroup - sortB.runGroup;
  if (sortA.phase !== sortB.phase) return sortA.phase - sortB.phase;
  if (sortA.sequence !== sortB.sequence) return sortA.sequence - sortB.sequence;
  if (sortA.time !== sortB.time) return sortA.time - sortB.time;
  if (sortA.index !== sortB.index) return sortA.index - sortB.index;
  return String(a.key).localeCompare(String(b.key));
}

function computeRunStarts(messages, activities) {
  const starts = new Map();
  const consider = (runId, time) => {
    if (!runId) return;
    const value = timeValue(time);
    if (!starts.has(runId) || value < starts.get(runId)) starts.set(runId, value);
  };
  activities.forEach((activity) => consider(numericValue(activity.run_id), activity.created_at));
  messages.forEach((message) => consider(numericValue(message.run_id), message.created_at));
  return Array.from(starts.entries())
    .map(([runId, time]) => ({ runId, time }))
    .sort((a, b) => a.runId - b.runId);
}

// A run-less message (e.g. a slash-command exchange) belongs chronologically
// right after the run that was active when it was sent, not at the very end
// of the transcript, which is where the giant NO_RUN_GROUP value pushed it.
function nearestRunGroup(time, runStarts) {
  let group = 0.5;
  for (const start of runStarts) {
    if (start.time <= time) group = start.runId + 0.5;
    else break;
  }
  return group;
}

export function buildTerminalItems(agentId, messages, activities, pendingItems) {
  const agentMessages = messages[agentId] || [];
  const agentActivities = activities[agentId] || [];
  const runStarts = computeRunStarts(agentMessages, agentActivities);
  const contextActivityByMessageId = new Map();
  agentActivities.forEach((activity) => {
    if (activity.type !== "context_injected") return;
    const messageId = numericValue(activity.payload?.message_id);
    if (messageId) contextActivityByMessageId.set(messageId, activity);
  });

  const terminalMessages = agentMessages.map((message, index) => {
    const sort = messageSort(message, index, contextActivityByMessageId.get(numericValue(message.id)));
    if (!numericValue(message.run_id) && sort.runGroup >= NO_RUN_GROUP) {
      sort.runGroup = nearestRunGroup(timeValue(message.created_at), runStarts);
    }
    return {
      kind: "message",
      key: `message-${message.id}`,
      created_at: message.created_at,
      sort,
      message
    };
  });
  const terminalActivities = agentActivities.map((activity, index) => ({
    kind: "activity",
    key: `activity-${activity.id}`,
    created_at: activity.created_at,
    sort: activitySort(activity, index),
    activity
  }));
  const rawPending = pendingItems[agentId] || [];
  const pendingMatches = confirmedPendingMatches(rawPending, agentMessages);
  const terminalPending = filterConfirmedPendingItems(rawPending, agentMessages, agentActivities)
    .map((item, index) => ({
      ...item,
      sort: item.sort || pendingSortWithMatch(item, index, pendingMatches.get(itemPendingToken(item)))
    }));
  const ordered = [...terminalMessages, ...terminalActivities, ...terminalPending].sort(compareTerminalItems);
  // Collapse a run of consecutive "plan updated" rows into a single entry.
  // Flash re-plans on almost every loop, so plan_progress events otherwise
  // spam the transcript with successive identical-looking rows; the plan pane
  // already shows the live plan, so only the latest marker needs to remain.
  const collapsed = [];
  for (const item of ordered) {
    const isPlan = item.kind === "activity" && item.activity?.type === "plan_progress";
    const prev = collapsed[collapsed.length - 1];
    const prevIsPlan = prev && prev.kind === "activity" && prev.activity?.type === "plan_progress";
    if (isPlan && prevIsPlan) {
      collapsed[collapsed.length - 1] = item;
      continue;
    }
    collapsed.push(item);
  }
  // Window the transcript to the most recent items. Rendering an unbounded
  // history re-ran (and re-mounted) hundreds of DOM nodes on every 4s poll,
  // growing memory until the tab crashed with Out-of-Memory. Older items are
  // still in the database and the run artifact; only the live view is capped.
  return collapsed.length > MAX_TERMINAL_ITEMS ? collapsed.slice(-MAX_TERMINAL_ITEMS) : collapsed;
}
