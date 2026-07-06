import { useCallback, useEffect, useRef, useState } from "react";
import { filterConfirmedPendingItems, messageKey, wheelCanScroll } from "../lib/terminalItems.js";

export function useTranscriptController({ cols, setRowHeight, setPendingItems }) {
  const [transcriptNeedsLatest, setTranscriptNeedsLatest] = useState({});
  const gridRef = useRef(null);
  const transcriptRefs = useRef({});
  const transcriptPinned = useRef({});
  const messageSigs = useRef({});
  // Typing is animated inside TypedMessage (one message re-renders per tick).
  // These refs only remember WHICH messages are new enough to animate.
  const knownMessageKeys = useRef(new Set());
  const animatedMessageKeys = useRef(new Set());

  const syncGeometry = useCallback(() => {
    const grid = gridRef.current;
    if (!grid) return;
    const gap = 1;
    const width = grid.clientWidth || 0;
    const raw = cols > 0 ? Math.floor((width - (cols - 1) * gap) / cols) : 500;
    setRowHeight(Math.max(380, Math.min(raw, 520)));
  }, [cols, setRowHeight]);

  const isTranscriptAtBottom = useCallback((node) => {
    if (!node) return true;
    return node.scrollHeight - node.scrollTop - node.clientHeight <= 24;
  }, []);

  const setTranscriptNeedsLatestFlag = useCallback((agentId, needsLatest) => {
    setTranscriptNeedsLatest((prev) => {
      if (prev[agentId] === needsLatest) return prev;
      return { ...prev, [agentId]: needsLatest };
    });
  }, []);

  const syncTranscripts = useCallback(() => {
    Object.entries(transcriptRefs.current).forEach(([agentId, node]) => {
      if (!node) return;
      if (transcriptPinned.current[agentId] !== false) {
        node.scrollTop = node.scrollHeight;
        transcriptPinned.current[agentId] = true;
        setTranscriptNeedsLatestFlag(agentId, false);
      } else {
        setTranscriptNeedsLatestFlag(agentId, !isTranscriptAtBottom(node));
      }
    });
  }, [isTranscriptAtBottom, setTranscriptNeedsLatestFlag]);

  const containScrollWheel = useCallback((event) => {
    if (wheelCanScroll(event.currentTarget, event.deltaY)) {
      event.stopPropagation();
    }
  }, []);

  const handleTranscriptScroll = useCallback((agentId) => {
    const node = transcriptRefs.current[agentId];
    const atBottom = isTranscriptAtBottom(node);
    transcriptPinned.current[agentId] = atBottom;
    setTranscriptNeedsLatestFlag(agentId, !atBottom);
  }, [isTranscriptAtBottom, setTranscriptNeedsLatestFlag]);

  const jumpTranscriptToLatest = useCallback((agentId) => {
    const node = transcriptRefs.current[agentId];
    if (!node) return;
    transcriptPinned.current[agentId] = true;
    node.scrollTop = node.scrollHeight;
    setTranscriptNeedsLatestFlag(agentId, false);
  }, [setTranscriptNeedsLatestFlag]);

  const prepareTyping = useCallback((nextMessages, firstLoad) => {
    for (const message of nextMessages) {
      if (message.role !== "assistant") continue;
      const key = messageKey(message);
      if (knownMessageKeys.current.has(key)) continue;
      knownMessageKeys.current.add(key);
      // Messages present on first load appear instantly; only messages that
      // arrive while the user watches get the reveal animation.
      if (!firstLoad) animatedMessageKeys.current.add(key);
    }
  }, []);

  const shouldAnimateMessage = useCallback((message) => {
    return message.role === "assistant" && animatedMessageKeys.current.has(messageKey(message));
  }, []);

  const finishMessageAnimation = useCallback((key) => {
    animatedMessageKeys.current.delete(key);
  }, []);

  const pruneConfirmedPendingItems = useCallback((agentId, nextMessages, nextActivities = []) => {
    setPendingItems((prev) => {
      const current = prev[agentId] || [];
      if (!current.length) return prev;
      const nextItems = filterConfirmedPendingItems(current, nextMessages, nextActivities);
      if (nextItems.length === current.length) return prev;
      const next = { ...prev };
      if (nextItems.length) next[agentId] = nextItems;
      else delete next[agentId];
      return next;
    });
  }, [setPendingItems]);

  const forgetAgents = useCallback((agentIds) => {
    agentIds.forEach((id) => {
      delete transcriptRefs.current[id];
      delete transcriptPinned.current[id];
      delete messageSigs.current[id];
    });
  }, []);

  const pinAgent = useCallback((agentId) => {
    transcriptPinned.current[agentId] = true;
    setTranscriptNeedsLatestFlag(agentId, false);
  }, [setTranscriptNeedsLatestFlag]);

  useEffect(() => {
    const onResize = () => syncGeometry();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [syncGeometry]);

  useEffect(() => syncGeometry(), [cols, syncGeometry]);

  return {
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
  };
}
