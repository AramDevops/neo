import { useEffect, useRef, useState } from "react";

export function useConnectionStatus() {
  const [connectionStatus, setConnectionStatus] = useState(null);
  const delayedRetry = useRef(0);
  const retryToken = useRef(0);

  useEffect(() => {
    let clearTimer = 0;
    const scheduleClear = () => {
      window.clearTimeout(clearTimer);
      clearTimer = window.setTimeout(() => setConnectionStatus(null), 2200);
    };
    const clearDelayedRetry = () => {
      window.clearTimeout(delayedRetry.current);
      delayedRetry.current = 0;
    };
    const onRetry = (event) => {
      window.clearTimeout(clearTimer);
      clearDelayedRetry();
      const detail = event.detail || {};
      const token = retryToken.current + 1;
      retryToken.current = token;
      const showRetry = () => {
        if (retryToken.current !== token) return;
        setConnectionStatus({ phase: "retry", ...detail });
      };
      if (Number(detail.attempt || 0) > 1) showRetry();
      else delayedRetry.current = window.setTimeout(showRetry, 900);
    };
    const onRecovered = () => {
      retryToken.current += 1;
      clearDelayedRetry();
      setConnectionStatus(null);
    };
    const onFailed = (event) => {
      retryToken.current += 1;
      clearDelayedRetry();
      setConnectionStatus({ phase: "failed", ...(event.detail || {}) });
      scheduleClear();
    };
    window.addEventListener("neo-api-retry", onRetry);
    window.addEventListener("neo-api-recovered", onRecovered);
    window.addEventListener("neo-api-failed", onFailed);
    return () => {
      window.clearTimeout(clearTimer);
      clearDelayedRetry();
      window.removeEventListener("neo-api-retry", onRetry);
      window.removeEventListener("neo-api-recovered", onRecovered);
      window.removeEventListener("neo-api-failed", onFailed);
    };
  }, []);

  return connectionStatus;
}
