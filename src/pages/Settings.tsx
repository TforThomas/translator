import { useEffect, useMemo, useState } from "react";
import {
  Loader2,
  CheckCircle2,
  Eye,
  EyeOff,
  TestTube,
  Server,
  Bot,
  Sparkles,
  Cpu,
  Wrench,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { apiGet, apiPostJson } from "../lib/api";

interface SettingsData {
  openai_api_key: string;
  openai_base_url: string;
  model_name: string;
}

interface ApiPreset {
  id: string;
  name: string;
  baseUrl: string;
  models: string[];
  icon: LucideIcon;
  apiKeyPlaceholder: string;
  description: string;
}

const API_PRESETS: ApiPreset[] = [
  {
    id: "siliconflow",
    name: "硅基流动",
    baseUrl: "https://api.siliconflow.cn/v1",
    models: ["deepseek-ai/DeepSeek-V3.2", "Qwen/Qwen2.5-72B-Instruct", "THUDM/glm-4-9b-chat", "meta-llama/Meta-Llama-3.1-405B-Instruct"],
    icon: Sparkles,
    apiKeyPlaceholder: "sf-...",
    description: "国内高速，支持多种开源模型",
  },
  {
    id: "openai",
    name: "OpenAI",
    baseUrl: "https://api.openai.com/v1",
    models: ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"],
    icon: Bot,
    apiKeyPlaceholder: "sk-...",
    description: "官方 GPT 系列模型",
  },
  {
    id: "google",
    name: "Google Gemini",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta",
    models: ["gemini-2.0-flash", "gemini-2.5-pro", "gemini-1.5-pro"],
    icon: Server,
    apiKeyPlaceholder: "AIzaSy...",
    description: "Google 原生 API",
  },
  {
    id: "ollama",
    name: "Ollama 本地",
    baseUrl: "http://localhost:11434/v1",
    models: ["llama3", "qwen2.5", "deepseek-r1", "mistral"],
    icon: Cpu,
    apiKeyPlaceholder: "本地模式可留空",
    description: "本地部署，离线可用",
  },
  {
    id: "custom",
    name: "自定义",
    baseUrl: "",
    models: [],
    icon: Wrench,
    apiKeyPlaceholder: "sk-...",
    description: "兼容 OpenAI 接口规范",
  },
];

export default function Settings() {
  const [settings, setSettings] = useState<SettingsData>({
    openai_api_key: "",
    openai_base_url: "https://api.siliconflow.cn/v1",
    model_name: "deepseek-ai/DeepSeek-V3.2",
  });
  const [selectedPreset, setSelectedPreset] = useState("siliconflow");
  const [isSaving, setIsSaving] = useState(false);
  const [isSaved, setIsSaved] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showApiKey, setShowApiKey] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

  useEffect(() => {
    const fetchSettings = async () => {
      try {
        const data = await apiGet<SettingsData>("/api/settings");
        setSettings(data);
        const matchedPreset = API_PRESETS.find((preset) => data.openai_base_url === preset.baseUrl);
        setSelectedPreset(matchedPreset ? matchedPreset.id : "custom");
      } catch (e) {
        setError("加载设置失败，请检查后端连接");
        console.error(e);
      } finally {
        setIsLoading(false);
      }
    };
    void fetchSettings();
  }, []);

  const selectedPresetData = useMemo(() => API_PRESETS.find((p) => p.id === selectedPreset), [selectedPreset]);

  const handlePresetChange = (presetId: string) => {
    setSelectedPreset(presetId);
    const preset = API_PRESETS.find((p) => p.id === presetId);
    if (preset) {
      setSettings((prev) => ({
        ...prev,
        openai_base_url: preset.baseUrl,
        model_name: preset.models.length > 0 ? preset.models[0] : "",
      }));
    }
  };

  const handleSave = async () => {
    if (!settings.openai_api_key && selectedPreset !== "ollama") {
      setError("API Key 不能为空");
      return;
    }

    setIsSaving(true);
    setIsSaved(false);
    setError(null);

    try {
      await apiPostJson("/api/settings", settings);
      setIsSaved(true);
      setTestResult(null);
      setTimeout(() => setIsSaved(false), 3000);
    } catch (e) {
      setError("保存设置失败");
      console.error(e);
    } finally {
      setIsSaving(false);
    }
  };

  const handleTestConnection = async () => {
    const isOllama = settings.openai_base_url.includes("localhost:11434") || settings.openai_base_url.includes("127.0.0.1:11434");

    if (!isOllama && !settings.openai_api_key) {
      setError("请先填写 API Key");
      return;
    }

    setIsTesting(true);
    setTestResult(null);
    setError(null);

    try {
      const isGoogle = settings.openai_base_url.includes("googleapis.com");

      let testResponse;
      if (isGoogle) {
        testResponse = await fetch(`${settings.openai_base_url}/models/${settings.model_name}:generateContent?key=${settings.openai_api_key}`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            contents: [
              {
                parts: [{ text: "Reply with 'OK' if you received this message." }],
              },
            ],
            generationConfig: {
              temperature: 0.1,
              maxOutputTokens: 10,
            },
          }),
        });
      } else {
        const headers: Record<string, string> = {
          "Content-Type": "application/json",
        };

        if (!isOllama) {
          headers.Authorization = `Bearer ${settings.openai_api_key}`;
        }

        testResponse = await fetch(`${settings.openai_base_url}/chat/completions`, {
          method: "POST",
          headers,
          body: JSON.stringify({
            model: settings.model_name,
            messages: [{ role: "user", content: "Reply with 'OK' if you received this message." }],
            max_tokens: 10,
          }),
        });
      }

      if (testResponse.ok) {
        setTestResult({
          success: true,
          message: "连接成功，API 与模型配置可用。",
        });
      } else {
        const errorData = await testResponse.json().catch(() => ({ error: { message: "未知错误" } }));
        setTestResult({
          success: false,
          message: `连接失败: ${errorData.error?.message || errorData.message || "请检查 API Key 和 Base URL"}`,
        });
      }
    } catch (e) {
      setTestResult({
        success: false,
        message: e instanceof Error ? e.message : "测试失败",
      });
    } finally {
      setIsTesting(false);
    }
  };

  if (isLoading) {
    return (
      <div className="panel flex h-64 items-center justify-center">
        <Loader2 size={28} className="animate-spin text-slate-400" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <section className="page-header flex-col items-start justify-center gap-2">
        <h1 className="text-2xl font-semibold tracking-tight text-slate-900">系统设置</h1>
        <p className="text-sm text-slate-600">配置翻译服务商、API Key 与模型参数。</p>
      </section>

      {error && <div className="alert-danger">{error}</div>}

      {isSaved && (
        <div className="inline-flex h-10 items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 px-4 text-sm text-emerald-700">
          <CheckCircle2 size={16} />
          设置已保存
        </div>
      )}

      {testResult && (
        <div
          className={`inline-flex h-10 items-center gap-2 rounded-xl border px-4 text-sm ${
            testResult.success ? "border-emerald-200 bg-emerald-50 text-emerald-700" : "border-rose-200 bg-rose-50 text-rose-700"
          }`}
        >
          <CheckCircle2 size={16} />
          {testResult.message}
        </div>
      )}

      <section className="panel p-6">
        <div className="space-y-6">
          <div>
            <label className="field-label">API 服务商</label>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {API_PRESETS.map((preset) => {
                const Icon = preset.icon;
                const active = selectedPreset === preset.id;
                return (
                  <button
                    key={preset.id}
                    onClick={() => handlePresetChange(preset.id)}
                    className={`min-h-[86px] rounded-xl border p-4 text-left transition ${
                      active ? "border-[rgb(var(--primary))] bg-[rgb(var(--primary-soft))]" : "border-[rgb(var(--line))] bg-white hover:bg-slate-50"
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <Icon size={16} className={active ? "text-[rgb(var(--primary))]" : "text-slate-500"} />
                      <p className="text-sm font-semibold text-slate-900">{preset.name}</p>
                    </div>
                    <p className="mt-1 text-xs text-slate-600">{preset.description}</p>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="grid gap-5 md:grid-cols-2">
            <label>
              <span className="field-label">API Key</span>
              <div className="relative">
                <input
                  type={showApiKey ? "text" : "password"}
                  value={settings.openai_api_key}
                  onChange={(e) => setSettings({ ...settings, openai_api_key: e.target.value })}
                  placeholder={selectedPresetData?.apiKeyPlaceholder || "sk-..."}
                  className="input-base pl-3 pr-10"
                />
                <button
                  type="button"
                  onClick={() => setShowApiKey(!showApiKey)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 rounded-md p-1 text-slate-500 transition hover:bg-slate-100 hover:text-slate-700"
                >
                  {showApiKey ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
              <p className="mt-1 text-xs text-slate-500">
                {selectedPreset === "google"
                  ? "Google API Key 可在 AI Studio 获取。"
                  : selectedPreset === "siliconflow"
                  ? "硅基流动 Key 可在控制台创建。"
                  : selectedPreset === "ollama"
                  ? "Ollama 本地模式通常无需 Key。"
                  : "API Key 将保存在本地数据库中。"}
              </p>
            </label>

            <label>
              <span className="field-label">API Base URL</span>
              <input
                value={settings.openai_base_url}
                onChange={(e) => setSettings({ ...settings, openai_base_url: e.target.value })}
                placeholder="https://api.siliconflow.cn/v1"
                className="input-base"
              />
              <p className="mt-1 text-xs text-slate-500">支持兼容 OpenAI API 协议的服务。</p>
            </label>
          </div>

          <div>
            <label className="field-label">Model Name</label>
            {selectedPreset !== "custom" && selectedPresetData?.models ? (
              <div className="space-y-3">
                <div className="flex flex-wrap gap-2">
                  {selectedPresetData.models.map((model) => (
                    <button
                      key={model}
                      onClick={() => setSettings({ ...settings, model_name: model })}
                      className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition ${
                        settings.model_name === model
                          ? "border-[rgb(var(--primary))] bg-[rgb(var(--primary-soft))] text-[rgb(var(--primary))]"
                          : "border-[rgb(var(--line))] text-slate-700 hover:bg-slate-50"
                      }`}
                    >
                      {model}
                    </button>
                  ))}
                </div>
                <input
                  value={settings.model_name}
                  onChange={(e) => setSettings({ ...settings, model_name: e.target.value })}
                  placeholder="或手动输入模型名称"
                  className="input-base"
                />
              </div>
            ) : (
              <input
                value={settings.model_name}
                onChange={(e) => setSettings({ ...settings, model_name: e.target.value })}
                placeholder="gpt-4o-mini"
                className="input-base"
              />
            )}
          </div>

          <div className="flex flex-wrap justify-end gap-3 border-t border-[rgb(var(--line))] pt-5">
            <button
              onClick={handleTestConnection}
              disabled={isTesting}
              className="btn-secondary px-5"
            >
              {isTesting ? <Loader2 size={16} className="animate-spin" /> : <TestTube size={16} />}
              {isTesting ? "测试中..." : "测试连接"}
            </button>

            <button
              onClick={handleSave}
              disabled={isSaving}
              className="btn-primary px-5"
            >
              {isSaving ? <Loader2 size={16} className="animate-spin" /> : <CheckCircle2 size={16} />}
              {isSaving ? "保存中..." : "保存设置"}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}