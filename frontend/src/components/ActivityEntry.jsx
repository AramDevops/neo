import { activityArgs, activityFileChange, activityOutput, activityScreenshot, activityTitle } from "../lib/activities.js";

export function ActivityEntry({ agent, item, expanded, onToggle, openVscode, openScreenshot, onMediaLoad }) {
  const change = activityFileChange(item.activity);
  const shot = activityScreenshot(item.activity);
  const thinking = item.activity.type === "model_response" || item.activity.type === "ui_pending";
  return (
    <div
      className={`tool-entry ${expanded ? "open" : ""} ${thinking ? "thinking" : ""} ${item.activity.payload?.ok === false ? "failed" : ""}`}
      key={item.key}
    >
      <div className="tool-head-wrap">
        <button
          className="tool-head"
          type="button"
          aria-expanded={Boolean(expanded)}
          onClick={onToggle}
        >
          <span>activity@{agent.name} $</span>
          <strong>{activityTitle(item.activity)}</strong>
          <em>run #{item.activity.run_id}</em>
        </button>
        {change && (
          <div className="tool-change">
            <button className="tool-file-link" type="button" onClick={() => openVscode(change.relativePath)} title={change.relativePath}>
              {change.fileName}
            </button>
            <span>{change.filesChanged} {change.filesChanged === 1 ? "file" : "files"} changed</span>
            <span className="diff-add">+{change.added}</span>
            <span className="diff-del">-{change.removed}</span>
            <button className="tool-review" type="button" onClick={() => openVscode(change.relativePath)} title={`open ${change.relativePath} in VS Code`}>Review</button>
          </div>
        )}
        {shot && (
          <button
            className="tool-shot"
            type="button"
            onClick={() => openScreenshot(item.activity)}
            title={`open ${shot.fileName} in gallery`}
          >
            {/* onLoad: the thumbnail grows the transcript after the scroll
                sync already ran; re-sync so a pinned view stays at bottom. */}
            <img src={shot.url} alt={`screenshot ${shot.fileName}`} loading="lazy" onLoad={onMediaLoad} />
          </button>
        )}
      </div>
      {expanded && (
        <div className="tool-body">
          {activityArgs(item.activity) && <pre className="tool-args">{activityArgs(item.activity)}</pre>}
          <pre className="tool-output">{activityOutput(item.activity)}</pre>
        </div>
      )}
    </div>
  );
}
