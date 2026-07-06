import ComputerAccessPanel from "./ComputerAccessPanel.jsx";

const SETTINGS_TABS = ["engine", "workspace", "context", "runs", "tools"];

function ApiKeyIcon({ visible }) {
  if (visible) {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M3 3l18 18" />
        <path d="M10.6 10.6a2 2 0 0 0 2.8 2.8" />
        <path d="M7.5 7.5C5.6 8.5 4 10 2.8 12c2.1 3.4 5.2 5.2 9.2 5.2 1.3 0 2.5-.2 3.6-.6" />
        <path d="M14.2 5.1c3 .5 5.3 2.1 7 4.9-.6 1-1.3 1.9-2.1 2.6" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M2.8 12c2.1-3.4 5.2-5.2 9.2-5.2s7.1 1.8 9.2 5.2c-2.1 3.4-5.2 5.2-9.2 5.2S4.9 15.4 2.8 12z" />
      <circle cx="12" cy="12" r="2.6" />
    </svg>
  );
}

export function SettingsDrawer({
  containScrollWheel,
  provider,
  model,
  setSettingsOpen,
  settingsTab,
  setSettingsTab,
  providers,
  handleProviderChange,
  modelOptions,
  setModel,
  providerNeedsApiKey,
  providerApiKey,
  setProviderApiKey,
  setProviderApiKeyDirty,
  providerApiKeyVisible,
  toggleProviderApiKeyVisible,
  providerShowsEndpoint,
  providerBaseUrl,
  setProviderBaseUrl,
  providerModelsText,
  setProviderModelsText,
  cols,
  setCols,
  providerInfo,
  workspace,
  workspaceDraft,
  setWorkspaceDraft,
  setWorkspaceDirty,
  setWorkspaceError,
  pickWorkspace,
  workspacePicking,
  setWorkspace,
  recentWorkspaces,
  fillWorkspace,
  workspaceError,
  contextDraft,
  setContextDraft,
  injectContext,
  sharedContext,
  normalizeAgentText,
  shortTime,
  runs,
  loadRun,
  runAgentLabel,
  traceText,
  toolGroups,
  dbLabel
}) {
  return (
    <aside className="settings-drawer" onWheel={containScrollWheel}>
      <div className="drawer-head">
        <div>
          <strong>settings</strong>
          <span className="muted">{provider}:{model}</span>
        </div>
        <button type="button" onClick={() => setSettingsOpen(false)}>close</button>
      </div>

      <nav className="settings-tabs" aria-label="settings sections">
        {SETTINGS_TABS.map((tab) => (
          <button className={settingsTab === tab ? "active" : ""} type="button" onClick={() => setSettingsTab(tab)} key={tab}>
            {tab === "workspace" ? "workdir" : tab}
          </button>
        ))}
      </nav>

      <div className="settings-body">
        {settingsTab === "engine" && (
          <section className="settings-page">
            <div className="settings-grid">
              <label className="field">
                <span>provider</span>
                <select value={provider} onChange={(event) => handleProviderChange(event.target.value)}>
                  {providers().map((item) => (
                    <option value={item.id} key={item.id}>{item.label} · {item.status}</option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span>model</span>
                <select value={model} onChange={(event) => setModel(event.target.value)}>
                  {modelOptions().map((item) => <option value={item} key={item}>{item}</option>)}
                </select>
              </label>
              {providerNeedsApiKey() && (
                <label className="field">
                  <span>api key</span>
                  <div className="api-key-control">
                    <input
                      value={providerApiKey}
                      onChange={(event) => {
                        setProviderApiKey(event.target.value);
                        setProviderApiKeyDirty(true);
                      }}
                      type={providerApiKeyVisible ? "text" : "password"}
                      autoComplete="off"
                      placeholder={providerInfo()?.has_api_key ? providerInfo()?.api_key_preview || "saved key" : "required"}
                    />
                    <button
                      className="api-key-toggle"
                      type="button"
                      onClick={toggleProviderApiKeyVisible}
                      aria-label={providerApiKeyVisible ? "hide api key" : "show api key"}
                      title={providerApiKeyVisible ? "hide api key" : "show api key"}
                    >
                      <ApiKeyIcon visible={providerApiKeyVisible} />
                    </button>
                    <button
                      className="api-key-clear"
                      type="button"
                      disabled={!providerInfo()?.has_api_key}
                      onClick={() => {
                        setProviderApiKey("");
                        setProviderApiKeyDirty(true);
                      }}
                    >
                      clear
                    </button>
                  </div>
                </label>
              )}
              {providerShowsEndpoint() && (
                <label className="field">
                  <span>endpoint</span>
                  <input
                    value={providerBaseUrl}
                    onChange={(event) => setProviderBaseUrl(event.target.value)}
                    type="text"
                    autoComplete="off"
                    placeholder={provider === "local" ? "http://127.0.0.1:11434/v1" : "provider default"}
                  />
                </label>
              )}
              <label className="field engine-models-field">
                <span>models</span>
                <textarea
                  value={providerModelsText}
                  onChange={(event) => setProviderModelsText(event.target.value)}
                  rows={5}
                  spellCheck={false}
                />
              </label>
              <label className="field">
                <span>grid columns</span>
                <select value={cols} onChange={(event) => setCols(Number(event.target.value))}>
                  <option value={2}>2</option>
                  <option value={3}>3</option>
                  <option value={4}>4</option>
                  <option value={1}>1</option>
                </select>
              </label>
            </div>
            <div className="provider-card">
              <span>{providerInfo()?.label || provider}</span>
              <strong>{provider}:{model}</strong>
              {providerInfo()?.base_url && <em>{providerInfo().base_url}</em>}
              <p>{providerInfo()?.notes || "Dynamic provider from the backend model registry."}</p>
            </div>
          </section>
        )}

        {settingsTab === "workspace" && (
          <section className="settings-page">
            <div className="workspace-status">
              <span>active</span>
              <strong>{workspace.workspace_dir || "not set"}</strong>
              <em>{workspace.writable ? "writable" : "not writable"}</em>
            </div>
            <form className="workspace-form" onSubmit={(event) => { event.preventDefault(); setWorkspace(); }}>
              <label className="field">
                <span>agent working directory</span>
                <input
                  value={workspaceDraft}
                  onChange={(event) => {
                    setWorkspaceDraft(event.target.value);
                    setWorkspaceDirty(true);
                    setWorkspaceError("");
                  }}
                  type="text"
                  autoComplete="off"
                  list="neo-workspace-recents"
                  placeholder="C:\\path\\to\\project"
                />
                <datalist id="neo-workspace-recents">
                  {recentWorkspaces.map((path) => <option value={path} key={path} />)}
                </datalist>
              </label>
              <div className="workspace-actions">
                <button type="button" onClick={pickWorkspace} disabled={workspacePicking}>
                  {workspacePicking ? "opening" : "browse"}
                </button>
                <button type="submit">set workdir</button>
              </div>
            </form>
            {recentWorkspaces.length > 0 && (
              <div className="workspace-recents">
                <span>recent</span>
                {recentWorkspaces.map((path) => (
                  <button type="button" onClick={() => fillWorkspace(path)} title={path} key={path}>{path}</button>
                ))}
              </div>
            )}
            {workspaceError && <div className="setting-error">{workspaceError}</div>}
            <div className="setting-note">Browse opens the native folder picker from the local Flask backend. Recent paths are saved in this browser localStorage.</div>
            <ComputerAccessPanel />
          </section>
        )}

        {settingsTab === "context" && (
          <section className="settings-page context-page">
            <form className="context-form" onSubmit={(event) => { event.preventDefault(); injectContext(); }}>
              <textarea rows={3} placeholder="shared context for every terminal" value={contextDraft} onChange={(event) => setContextDraft(event.target.value)} />
              <button type="submit">inject context</button>
            </form>
            <div className="context-list">
              {sharedContext.map((item) => (
                <div className="context-item" key={item.id}>
                  <span className="tag">{item.role}</span><span>{item.display_content || normalizeAgentText(item.content)}</span>
                  <div className="muted">{shortTime(item.created_at)}</div>
                </div>
              ))}
            </div>
          </section>
        )}

        {settingsTab === "runs" && (
          <section className="settings-page runs-page">
            <div className="run-list">
              {runs.map((run) => (
                <div className="run-item" onClick={() => loadRun(run.id)} key={run.id}>
                  <span className="tag">#{run.id}</span><span>{run.status} agent:{runAgentLabel(run)}</span>
                  <div className="muted">{run.provider}:{run.model} / {run.latency_ms || 0}ms / tools:{run.tool_count || 0}</div>
                </div>
              ))}
            </div>
            <pre className="trace-view">{traceText}</pre>
          </section>
        )}

        {settingsTab === "tools" && (
          <section className="settings-page tools-page">
            <div className="setting-note danger-note">Advanced tool catalog. Agents choose tools automatically; this list is for diagnostics.</div>
            <div className="setting-note diagnostics-line">
              <span>database</span>
              <strong>{dbLabel()}</strong>
            </div>
            {toolGroups().map((group) => (
              <details className="tool-group" key={group.title}>
                <summary><span>{group.title}</span><em>{group.items.length}</em></summary>
                <div className="tool-list">
                  {group.items.map((tool) => <span className="tool-item" title={tool.purpose} key={tool.name}>{tool.name}</span>)}
                </div>
              </details>
            ))}
          </section>
        )}
      </div>
    </aside>
  );
}
