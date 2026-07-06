import { NeoLogo } from "./NeoLogo.jsx";

export function TopBar({
  workspace,
  workspaceLabel,
  openSettings,
  createAgent,
  deleteAllAgents,
  deletingAll,
  hasRunningAgents,
  hasAgents,
  syncActive,
  toggleSync,
  aiRetryStatus
}) {
  return (
    <header className="topbar">
      <div className="brand">
        <NeoLogo />
        <button className="path-chip" type="button" onClick={() => openSettings("workspace")} title={workspace.workspace_dir || ""}>
          {workspaceLabel()}
        </button>
      </div>
      <div className="toolbar">
        {aiRetryStatus && (
          <span className={`connection-chip ${aiRetryStatus.phase || ""}`} title={aiRetryStatus.message || "AI provider retry"}>
            {aiRetryStatus.phase === "retry"
              ? `ai retry ${aiRetryStatus.attempt}/${aiRetryStatus.max}`
              : aiRetryStatus.phase}
          </span>
        )}
        <button type="button" onClick={createAgent}>+ terminal</button>
        <button
          className="danger-btn"
          type="button"
          onClick={deleteAllAgents}
          disabled={!hasAgents || deletingAll || hasRunningAgents}
          title={hasRunningAgents ? "running terminals must finish before deletion" : "delete all terminals"}
        >
          {deletingAll ? "deleting" : "delete all"}
        </button>
        <button className={syncActive ? "mode-active" : ""} type="button" onClick={toggleSync}>{syncActive ? "sync on" : "sync off"}</button>
        <button type="button" onClick={() => openSettings()}>settings</button>
      </div>
    </header>
  );
}
