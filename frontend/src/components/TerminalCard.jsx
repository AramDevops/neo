import { useRef } from "react";
import { MessageText } from "./MessageText.jsx";
import { TypedMessage } from "./TypedMessage.jsx";
import { ActivityEntry } from "./ActivityEntry.jsx";
import SlashMenu from "./SlashMenu.jsx";
import { messageKey } from "../lib/terminalItems.js";

export function TerminalCard({
  agent,
  accent,
  hiddenPlan,
  togglePlan,
  clearAgent,
  clearingAgent,
  deleteAgent,
  deletingAgent,
  stopAgent,
  stoppingAgent,
  items,
  toolStats = { label: "0", total: 0 },
  normalizeAgentText,
  shouldAnimateMessage,
  finishMessageAnimation,
  syncTranscripts,
  transcriptRefs,
  handleTranscriptScroll,
  containScrollWheel,
  transcriptNeedsLatest,
  jumpTranscriptToLatest,
  plans,
  planState,
  pasteAttachments,
  removePasteAttachment,
  drafts,
  setDrafts,
  handleTerminalKeyDown,
  handleTerminalPaste,
  handleTerminalDragOver,
  handleTerminalDragLeave,
  handleTerminalDrop,
  attachFiles,
  dropTarget,
  sendMessage,
  expandedActivities,
  toggleActivity,
  openVscode,
  screenshotCount = 0,
  openGallery,
  openScreenshot
}) {
  const fileInputRef = useRef(null);

  function pickFiles(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (files.length) attachFiles(agent.id, files);
  }

  return (
    <article
      className={`terminal ${hiddenPlan ? "plan-hidden" : ""} ${dropTarget ? "drop-target" : ""}`}
      style={{ "--accent": accent }}
      onDragOver={(event) => handleTerminalDragOver(event, agent.id)}
      onDragLeave={(event) => handleTerminalDragLeave(event, agent.id)}
      onDrop={(event) => handleTerminalDrop(event, agent.id)}
      key={agent.id}
    >
      <div className="terminal-head">
        <div className="agent-id">
          <div className="agent-name">{agent.name}</div>
          <span className="agent-meta">{agent.title || "agent"} / {agent.provider}:{agent.model}</span>
        </div>
        <div className="terminal-actions">
          <span className="tool-count" title={`${toolStats.total} total tool calls recorded for this terminal`}>
            tools {toolStats.label}
          </span>
          {screenshotCount > 0 && (
            <button
              className="mini-btn gallery-open"
              type="button"
              onClick={() => openGallery(agent.id)}
              title={`open ${screenshotCount} screenshot${screenshotCount === 1 ? "" : "s"} in gallery`}
            >
              shots {screenshotCount}
            </button>
          )}
          <button className={`mini-btn plan-toggle ${!hiddenPlan ? "active" : ""}`} type="button" onClick={() => togglePlan(agent.id)}>
            {hiddenPlan ? "plan" : "hide"}
          </button>
          <button
            className="mini-btn clear-chat"
            type="button"
            onClick={() => clearAgent(agent.id)}
            disabled={Boolean(clearingAgent) || agent.status === "running"}
            title={agent.status === "running" ? "running terminal must finish before clearing chat" : "clear chat"}
          >
            {clearingAgent ? "..." : "clear"}
          </button>
          <button
            className="mini-btn remove-agent"
            type="button"
            onClick={() => deleteAgent(agent.id)}
            disabled={Boolean(deletingAgent) || agent.status === "running"}
            title={agent.status === "running" ? "running terminal must finish before deletion" : "delete terminal"}
          >
            {deletingAgent ? "..." : "rm"}
          </button>
          <span className={`status ${agent.status}`}>{agent.status}</span>
        </div>
      </div>

      <div className="console-view">
        <div className="transcript-shell">
          <div
            className="transcript"
            ref={(node) => { transcriptRefs.current[agent.id] = node; }}
            onScroll={() => handleTranscriptScroll(agent.id)}
            onWheel={containScrollWheel}
          >
            {items.length === 0 && (
              <p className="line"><span className="role">system@{agent.name} $</span><span className="line-text">ready</span></p>
            )}
            {items.map((item) => item.kind === "message" ? (
              <p className={`line ${item.message.role}`} key={item.key}>
                <span className="role">{item.message.role}@{agent.name} $</span>
                {item.message.role === "assistant" ? (
                  <TypedMessage
                    messageKey={messageKey(item.message)}
                    content={normalizeAgentText(item.message.content || "")}
                    animate={shouldAnimateMessage(item.message)}
                    onDone={finishMessageAnimation}
                    onGrow={syncTranscripts}
                  />
                ) : (
                  <MessageText content={normalizeAgentText(item.message.content || "")} />
                )}
              </p>
            ) : (
              <ActivityEntry
                agent={agent}
                item={item}
                expanded={Boolean(expandedActivities[item.key])}
                onToggle={() => toggleActivity(item.key)}
                openVscode={openVscode}
                openScreenshot={(activity) => openScreenshot(agent.id, activity)}
                onMediaLoad={syncTranscripts}
                key={item.key}
              />
            ))}
          </div>
          {transcriptNeedsLatest && (
            <button
              className="latest-jump"
              type="button"
              aria-label={`Jump ${agent.name} transcript to latest`}
              title="jump to latest"
              onClick={() => jumpTranscriptToLatest(agent.id)}
            >
              ↓
            </button>
          )}
        </div>

        <aside className="plan-pane" onWheel={containScrollWheel}>
          <div className="plan-title">
            <span>plan</span>
            <span>{planState(agent.id)}</span>
          </div>
          <div className="plan-pipeline">
            {(plans[agent.id] || []).length === 0 ? (
              <div className="plan-step idle"><span className="state-dot" /><span className="step-text">idle</span></div>
            ) : (plans[agent.id] || []).map((plan) => (
              <div className={`plan-step ${plan.status || "pending"}`} key={plan.id || plan.step_text}>
                <span className="state-dot" />
                <span className="step-text">{plan.step_text}</span>
              </div>
            ))}
          </div>
        </aside>
      </div>

      <form className={`terminal-form ${agent.status === "running" ? "has-stop" : ""}`} onSubmit={(event) => { event.preventDefault(); sendMessage(agent.id); }}>
        <SlashMenu
          draft={drafts[agent.id] || ""}
          onPick={(cmd) => setDrafts((prev) => ({ ...prev, [agent.id]: `${cmd} ` }))}
        />
        {(pasteAttachments[agent.id] || []).length > 0 && (
          <div className="paste-tray">
            {(pasteAttachments[agent.id] || []).map((item) => (
              <div className="paste-chip" key={item.id}>
                <span className="paste-icon">{item.kind === "file" ? ">>" : "::"}</span>
                <span className="paste-meta">
                  <strong>{item.title}</strong>
                  {item.kind === "file" ? (
                    <em>Attached file · {item.relativePath} · {Math.max(1, Math.round((item.size || 0) / 1024))} KB</em>
                  ) : (
                    <em>Adding pasted text · {item.chars} chars</em>
                  )}
                </span>
                <button type="button" aria-label="remove attachment" onClick={() => removePasteAttachment(agent.id, item.id)}>x</button>
              </div>
            ))}
          </div>
        )}
        <textarea
          rows={2}
          placeholder={agent.status === "running" ? "inject context" : "message"}
          value={drafts[agent.id] || ""}
          onChange={(event) => setDrafts((prev) => ({ ...prev, [agent.id]: event.target.value }))}
          onKeyDown={(event) => handleTerminalKeyDown(event, agent.id)}
          onPaste={(event) => handleTerminalPaste(event, agent.id)}
        />
        <input ref={fileInputRef} type="file" multiple className="attach-input" onChange={pickFiles} />
        <button
          type="button"
          className="attach-btn"
          title="attach files from disk (or drag & drop onto the terminal)"
          aria-label={`attach files to ${agent.name}`}
          onClick={() => fileInputRef.current?.click()}
        >
          +
        </button>
        <button type="submit">{agent.status === "running" ? "add" : "send"}</button>
        {agent.status === "running" && (
          <button
            type="button"
            className="stop-btn"
            onClick={() => stopAgent(agent.id)}
            disabled={stoppingAgent}
            title={stoppingAgent ? "stopping at the next safe point (a tool call in flight finishes first)" : "stop this run"}
            aria-label={`stop ${agent.name}`}
          >
            {stoppingAgent ? "..." : "stop"}
          </button>
        )}
      </form>
    </article>
  );
}
