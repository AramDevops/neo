export function normalizeModelList(models) {
  const values = Array.isArray(models) ? models : String(models || "").split(/[\n,]/);
  const seen = new Set();
  return values
    .map((item) => String(item || "").trim())
    .filter((item) => {
      if (!item || seen.has(item)) return false;
      seen.add(item);
      return true;
    });
}

export function modelListText(models) {
  return normalizeModelList(models).join("\n");
}

export function parseModelList(text) {
  return normalizeModelList(String(text || "").split(/[\n,]/));
}
