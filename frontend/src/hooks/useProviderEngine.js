import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api.js";
import { modelListText, parseModelList } from "../lib/providerModels.js";
import {
  friendlyProviderError,
  modelOptionsForProvider,
  providerInfoFromList,
  providerNeedsApiKey,
  providerShowsEndpoint,
  providersFromCatalog
} from "../lib/viewModels.js";

export function useProviderEngine() {
  const [modelCatalog, setModelCatalog] = useState({ providers: [] });
  const [provider, setProvider] = useState("gemini");
  const [model, setModel] = useState("gemini-3.5-flash");
  const [providerApiKey, setProviderApiKey] = useState("");
  const [providerApiKeyDirty, setProviderApiKeyDirty] = useState(false);
  const [providerApiKeyVisible, setProviderApiKeyVisible] = useState(false);
  const [providerBaseUrl, setProviderBaseUrl] = useState("");
  const [providerModelsText, setProviderModelsText] = useState("");
  const hydrated = useRef(false);
  const engineAutoSignature = useRef("");
  const providerAutoSignature = useRef("");
  const refreshAutoSignature = useRef("");

  function providers() { return providersFromCatalog(modelCatalog); }

  function providerInfo(providerId = provider) { return providerInfoFromList(providers(), providerId); }

  function providerNeedsKey(providerId = provider) { return providerNeedsApiKey(providerId); }

  function providerHasEndpoint(providerId = provider) { return providerShowsEndpoint(providerId); }

  function modelOptions() { return modelOptionsForProvider(providerInfo(), model); }

  const hydrate = useCallback((nextCatalog, config = {}) => {
    if (hydrated.current) return;
    const nextProvider = nextCatalog.default_provider || config.provider || "gemini";
    const nextModel = nextCatalog.default_model || config.model || "gemini-3.5-flash";
    const info = nextCatalog.providers?.find((item) => item.id === nextProvider) || nextCatalog.providers?.[0];
    setProvider(nextProvider);
    setModel(nextModel);
    setProviderBaseUrl(info?.base_url || "");
    setProviderModelsText(modelListText(info?.models || []));
    setProviderApiKey(info?.api_key || "");
    setProviderApiKeyDirty(false);
    setProviderApiKeyVisible(false);
    hydrated.current = true;
  }, []);

  function handleProviderChange(nextProvider) {
    const info = providerInfo(nextProvider);
    setProvider(nextProvider);
    if (info?.default_model) setModel(info.default_model);
    setProviderBaseUrl(info?.base_url || "");
    setProviderModelsText(modelListText(info?.models || []));
    setProviderApiKey(info?.api_key || "");
    setProviderApiKeyDirty(false);
    setProviderApiKeyVisible(false);
  }

  async function toggleProviderApiKeyVisible() {
    if (providerApiKeyVisible) {
      setProviderApiKeyVisible(false);
      return;
    }
    const info = providerInfo();
    if (!providerApiKey && !providerApiKeyDirty && info?.has_api_key) {
      try {
        const data = await api(`/api/providers/${provider}/api-key/reveal`, {
          method: "POST",
          retryUnsafe: true
        });
        setProviderApiKey(data.api_key || "");
        setProviderApiKeyDirty(false);
      } catch (error) {
        friendlyProviderError(error, "api key reveal failed");
      }
    }
    setProviderApiKeyVisible(true);
  }

  useEffect(() => {
    if (!hydrated.current || !provider || !model) return undefined;
    const signature = JSON.stringify({ provider, model });
    if (engineAutoSignature.current === signature) return undefined;
    const timer = window.setTimeout(async () => {
      try {
        const data = await api("/api/engine", {
          method: "POST",
          retryUnsafe: true,
          body: JSON.stringify({ provider, model })
        });
        engineAutoSignature.current = signature;
        setModelCatalog(data.model_catalog || modelCatalog);
      } catch (error) {
        friendlyProviderError(error, "engine auto-save failed");
      }
    }, 500);
    return () => window.clearTimeout(timer);
  }, [provider, model, modelCatalog, setModelCatalog]);

  useEffect(() => {
    if (!hydrated.current || !provider || provider === "mock") return undefined;
    const models = parseModelList(providerModelsText);
    const cleanKey = providerApiKey.trim();
    const shouldSaveKey = providerApiKeyDirty && cleanKey;
    const shouldClearKey = providerApiKeyDirty && !cleanKey && providerInfo()?.has_api_key;
    const baseUrl = providerBaseUrl.trim();
    const signature = JSON.stringify({
      provider,
      api_key: shouldSaveKey ? "provided" : shouldClearKey ? "clear" : "",
      base_url: baseUrl,
      models
    });
    if (providerAutoSignature.current === signature) return undefined;
    const timer = window.setTimeout(async () => {
      try {
        const payload = { base_url: baseUrl, models };
        if (shouldSaveKey) payload.api_key = cleanKey;
        if (shouldClearKey) payload.clear_api_key = true;
        const data = await api(`/api/providers/${provider}`, {
          method: "POST",
          retryUnsafe: true,
          body: JSON.stringify(payload)
        });
        const catalog = data.model_catalog || modelCatalog;
        const info = catalog.providers?.find((item) => item.id === provider);
        const savedModels = info?.models || models;
        const blankSignature = JSON.stringify({
          provider,
          api_key: "",
          base_url: baseUrl,
          models: savedModels
        });
        providerAutoSignature.current = shouldSaveKey || shouldClearKey ? blankSignature : signature;
        setModelCatalog(catalog);
        setProviderModelsText(modelListText(savedModels));
        if (shouldSaveKey || shouldClearKey) {
          setProviderApiKey(info?.api_key || "");
          setProviderApiKeyDirty(false);
        }
      } catch (error) {
        friendlyProviderError(error, "provider auto-save failed");
      }
    }, 800);
    return () => window.clearTimeout(timer);
  }, [provider, providerApiKey, providerApiKeyDirty, providerBaseUrl, providerModelsText, modelCatalog, setModelCatalog]);

  useEffect(() => {
    if (!hydrated.current || !provider || provider === "mock") return undefined;
    const info = modelCatalog.providers?.find((item) => item.id === provider);
    const cleanKey = providerApiKeyDirty ? providerApiKey.trim() : "";
    const baseUrl = providerBaseUrl.trim() || info?.base_url || "";
    const keyReady = Boolean(cleanKey || info?.has_api_key || provider === "local");
    if (!keyReady) return undefined;
    if (provider === "local" && !baseUrl) return undefined;
    const signature = JSON.stringify({
      provider,
      key: cleanKey ? "provided" : "saved",
      base_url: baseUrl
    });
    if (refreshAutoSignature.current === signature) return undefined;
    const timer = window.setTimeout(async () => {
      try {
        const payload = { base_url: baseUrl };
        if (cleanKey) payload.api_key = cleanKey;
        const data = await api(`/api/providers/${provider}/models/refresh`, {
          method: "POST",
          retryUnsafe: true,
          body: JSON.stringify(payload)
        });
        const catalog = data.model_catalog || modelCatalog;
        const models = data.result?.models || [];
        refreshAutoSignature.current = cleanKey
          ? JSON.stringify({ provider, key: "saved", base_url: baseUrl })
          : signature;
        setModelCatalog(catalog);
        if (models.length) {
          setProviderModelsText(modelListText(models));
          if (!models.includes(model)) setModel(models[0]);
        }
        if (cleanKey) {
          const refreshedInfo = catalog.providers?.find((item) => item.id === provider);
          setProviderApiKey(refreshedInfo?.api_key || "");
          setProviderApiKeyDirty(false);
        }
      } catch (error) {
        refreshAutoSignature.current = signature;
        friendlyProviderError(error, "model refresh unavailable");
      }
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [provider, providerApiKey, providerApiKeyDirty, providerBaseUrl, modelCatalog, model, setModelCatalog]);

  return {
    provider,
    model,
    setModel,
    providerApiKey,
    setProviderApiKey,
    setProviderApiKeyDirty,
    providerApiKeyVisible,
    setProviderApiKeyVisible,
    toggleProviderApiKeyVisible,
    providerBaseUrl,
    setProviderBaseUrl,
    providerModelsText,
    setProviderModelsText,
    providers,
    providerInfo,
    providerNeedsApiKey: providerNeedsKey,
    providerShowsEndpoint: providerHasEndpoint,
    modelOptions,
    handleProviderChange,
    hydrateProviderEngine: hydrate
  };
}
