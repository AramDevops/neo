import { useEffect } from "react";

export function ScreenshotGallery({ agent, shots, index, onSelect, onClose }) {
  const count = shots.length;
  const safeIndex = Math.min(Math.max(index, 0), count - 1);
  const current = shots[safeIndex];

  useEffect(() => {
    function onKey(event) {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowLeft" && safeIndex > 0) onSelect(safeIndex - 1);
      if (event.key === "ArrowRight" && safeIndex < count - 1) onSelect(safeIndex + 1);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [safeIndex, count, onSelect, onClose]);

  if (!current) return null;
  return (
    <div className="gallery-overlay" role="dialog" aria-modal="true" aria-label={`${agent?.name || "agent"} screenshots`} onClick={onClose}>
      <div className="gallery-frame" onClick={(event) => event.stopPropagation()}>
        <div className="gallery-head">
          <span className="gallery-title">{agent?.name || "agent"} / screenshots</span>
          <span className="gallery-counter">
            {safeIndex + 1}/{count}
            {current.runId ? ` · run #${current.runId}` : ""}
          </span>
          <a className="mini-btn" href={current.url} target="_blank" rel="noreferrer" title="open the PNG in a new tab">
            open file
          </a>
          <button className="mini-btn" type="button" onClick={onClose}>close</button>
        </div>
        <div className="gallery-stage">
          <button className="gallery-nav" type="button" disabled={safeIndex <= 0} onClick={() => onSelect(safeIndex - 1)} aria-label="previous screenshot">
            ‹
          </button>
          <img className="gallery-image" src={current.url} alt={`screenshot ${current.fileName}`} />
          <button className="gallery-nav" type="button" disabled={safeIndex >= count - 1} onClick={() => onSelect(safeIndex + 1)} aria-label="next screenshot">
            ›
          </button>
        </div>
        {count > 1 && (
          <div className="gallery-strip">
            {shots.map((shot, i) => (
              <button
                key={shot.activityId || shot.url}
                className={`gallery-thumb ${i === safeIndex ? "active" : ""}`}
                type="button"
                title={shot.fileName}
                onClick={() => onSelect(i)}
              >
                <img src={shot.url} alt="" loading="lazy" />
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
