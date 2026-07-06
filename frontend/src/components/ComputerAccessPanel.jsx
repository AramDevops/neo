import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api.js";

export default function ComputerAccessPanel() {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api("/api/computer-access"));
      setError("");
    } catch (err) {
      setError(err?.message || "could not load computer access status");
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const call = useCallback(async (path, body) => {
    setBusy(true);
    try {
      setStatus(await api(path, { method: "POST", body: JSON.stringify(body || {}) }));
      setError("");
    } catch (err) {
      setError(err?.message || "computer access request failed");
    } finally {
      setBusy(false);
    }
  }, []);

  const mode = status?.mode || "ask";
  const allowed = Boolean(status?.allowed);
  const remaining = Number(status?.seconds_remaining || 0);

  return (
    <div className="computer-access">
      <div className="workspace-status">
        <span>computer control</span>
        <strong>
          {mode === "full"
            ? "full control enabled"
            : allowed
              ? `granted for ${Math.max(1, Math.round(remaining / 60))} more min`
              : "ask mode - agents cannot see or use the screen"}
        </strong>
        <em>{mode === "full" ? "agents may capture the screen and send input anytime" : "grants are timed and revocable"}</em>
      </div>
      <div className="workspace-actions">
        <button
          type="button"
          disabled={busy}
          onClick={() => call("/api/computer-access/mode", { mode: mode === "full" ? "ask" : "full" })}
        >
          {mode === "full" ? "switch to ask mode" : "enable full control"}
        </button>
        {mode === "ask" && !allowed && (
          <button type="button" disabled={busy} onClick={() => call("/api/computer-access/grant", {})}>
            grant access
          </button>
        )}
        {mode === "ask" && allowed && (
          <button type="button" disabled={busy} onClick={() => call("/api/computer-access/revoke")}>
            revoke access
          </button>
        )}
      </div>
      {error && <div className="setting-error">{error}</div>}
      <div className="setting-note">
        Ask mode blocks screen capture, clicks, typing, and window control until you grant timed,
        revocable access. Full control removes that prompt for computer use only; the workspace
        sandbox, destructive-command blocklist, and read-only database access stay enforced in
        every mode.
      </div>
    </div>
  );
}
