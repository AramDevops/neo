import { memo, useEffect, useRef, useState } from "react";
import { MessageText } from "./MessageText.jsx";

// Char-by-char reveal isolated to a single message component. The previous
// implementation kept typing progress in App-level state, so every 18ms tick
// re-rendered the entire workbench (every terminal, every tool entry, full
// markdown re-parse); on large transcripts a render pass took longer than a
// tick and the UI froze. Here a tick re-renders exactly one message.
const TYPING_ANIMATION_MAX = 600;
const TICK_MS = 18;
const CHARS_PER_TICK = 4;

export const TypedMessage = memo(function TypedMessage({ messageKey, content, animate, onDone, onGrow }) {
  const full = content || "";
  const skip = !animate || full.length > TYPING_ANIMATION_MAX;
  const [visibleChars, setVisibleChars] = useState(skip ? full.length : 0);
  const doneRef = useRef(skip);
  const callbacksRef = useRef({ onDone, onGrow });
  callbacksRef.current = { onDone, onGrow };

  useEffect(() => {
    if (doneRef.current) return undefined;
    const timer = window.setInterval(() => {
      setVisibleChars((current) => {
        const next = Math.min(full.length, current + CHARS_PER_TICK);
        if (next >= full.length) {
          window.clearInterval(timer);
          if (!doneRef.current) {
            doneRef.current = true;
            callbacksRef.current.onDone?.(messageKey);
          }
        }
        return next;
      });
      callbacksRef.current.onGrow?.();
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, [full, messageKey]);

  const typing = !doneRef.current && visibleChars < full.length;
  return (
    <>
      <MessageText content={typing ? full.slice(0, visibleChars) : full} />
      {typing && <span className="cursor">_</span>}
    </>
  );
});
