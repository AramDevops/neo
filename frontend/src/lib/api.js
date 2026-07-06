const API_RETRY_MAX = 5;

function delay(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function emitApiEvent(name, detail) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(name, { detail }));
}

function retryableStatus(status) {
  return [408, 425, 429, 500, 502, 503, 504].includes(Number(status));
}

export async function apiUpload(path, formData) {
  // Separate from api(): multipart bodies must NOT get a manual Content-Type
  // header (the browser sets the boundary), and uploads are never retried.
  const response = await fetch(path, { method: "POST", body: formData });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || response.statusText);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

export async function api(path, options = {}) {
  const {
    retries = API_RETRY_MAX,
    retryUnsafe = false,
    ...fetchOptions
  } = options;
  const method = String(fetchOptions.method || "GET").toUpperCase();
  const canRetry = method === "GET" || method === "HEAD" || retryUnsafe;
  let lastError = null;

  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...fetchOptions
      });
      const data = await response.json().catch(() => ({}));
      if (response.ok) {
        if (attempt > 0) emitApiEvent("neo-api-recovered", { path, method });
        return data;
      }
      const error = new Error(data.error || response.statusText);
      error.status = response.status;
      error.data = data;
      if (!canRetry || attempt >= retries || !retryableStatus(response.status)) throw error;
      lastError = error;
    } catch (error) {
      if (error?.status || !canRetry || attempt >= retries) {
        if (attempt > 0) emitApiEvent("neo-api-failed", { path, method, message: error?.message || "connection lost" });
        throw error;
      }
      lastError = error;
    }
    const retryAttempt = attempt + 1;
    emitApiEvent("neo-api-retry", {
      path,
      method,
      attempt: retryAttempt,
      max: retries,
      message: lastError?.message || "connection lost"
    });
    await delay(Math.min(500 * (2 ** attempt), 4500));
  }
  throw lastError || new Error("request failed");
}
