import { useEffect, useMemo, useState } from "react";
import { Loader2, CheckCircle2, Eye, EyeOff, TestTube, Server, Bot, Sparkles, Cpu, Wrench } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { apiGet, apiPostJson } from "../lib/api";

interface SettingsData { openai_api_key: string; openai_base_url: string; model_name: string; }
interface ApiPreset { id: string; name: string; baseUrl: string; models: string[]; icon: LucideIcon; apiKeyPlaceholder: string; description: string; }

const API_PRESETS: ApiPreset[] = [
	{ id: "siliconflow", name: "硅基流动", baseUrl: "https://api.siliconflow.cn/v1", models: ["deepseek-ai/DeepSeek-V3.2", "Qwen/Qwen2.5-72B-Instruct", "THUDM/glm-4-9b-chat", "meta-llama/Meta-Llama-3.1-405B-Instruct"], icon: Sparkles, apiKeyPlaceholder: "sf-...", description: "国内高速，多种开源模型" },
	{ id: "openai", name: "OpenAI", baseUrl: "https://api.openai.com/v1", models: ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"], icon: Bot, apiKeyPlaceholder: "sk-...", description: "官方 GPT 系列模型" },
	{ id: "google", name: "Google Gemini", baseUrl: "https://generativelanguage.googleapis.com/v1beta", models: ["gemini-2.0-flash", "gemini-2.5-pro", "gemini-1.5-pro"], icon: Server, apiKeyPlaceholder: "AIzaSy...", description: "Google 原生 API" },
	{ id: "ollama", name: "Ollama 本地", baseUrl: "http://localhost:11434/v1", models: ["llama3", "qwen2.5", "deepseek-r1", "mistral"], icon: Cpu, apiKeyPlaceholder: "本地模式可留空", description: "本地部署，离线可用" },
	{ id: "custom", name: "自定义", baseUrl: "", models: [], icon: Wrench, apiKeyPlaceholder: "sk-...", description: "兼容 OpenAI 接口规范" },
];

export default function Settings() {
	const [settings, setSettings] = useState<SettingsData>({ openai_api_key: "", openai_base_url: "https://api.siliconflow.cn/v1", model_name: "deepseek-ai/DeepSeek-V3.2" });
	const [preset, setPreset] = useState("siliconflow");
	const [isSaving, setIsSaving] = useState(false);
	const [isSaved, setIsSaved] = useState(false);
	const [loading, setLoading] = useState(true);
	const [err, setErr] = useState<string | null>(null);
	const [showKey, setShowKey] = useState(false);
	const [testing, setTesting] = useState(false);
	const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

	useEffect(() => {
		(async () => {
			try {
				const d = await apiGet<SettingsData>("/api/settings");
				setSettings(d);
				const m = API_PRESETS.find((p) => d.openai_base_url === p.baseUrl);
				setPreset(m ? m.id : "custom");
			} catch { setErr("加载设置失败，请检查后端连接"); }
			finally { setLoading(false); }
		})();
	}, []);

	const data = useMemo(() => API_PRESETS.find((p) => p.id === preset), [preset]);

	const pickPreset = (pid: string) => {
		setPreset(pid);
		const p = API_PRESETS.find((x) => x.id === pid);
		if (p) setSettings((prev) => ({ ...prev, openai_base_url: p.baseUrl, model_name: p.models[0] || "" }));
	};

	const save = async () => {
		if (!settings.openai_api_key && preset !== "ollama") { setErr("API Key 不能为空"); return; }
		setIsSaving(true); setIsSaved(false); setErr(null);
		try {
			await apiPostJson("/api/settings", settings);
			setIsSaved(true); setTestResult(null);
			setTimeout(() => setIsSaved(false), 3000);
		} catch { setErr("保存设置失败"); }
		finally { setIsSaving(false); }
	};

	const test = async () => {
		const isOllama = settings.openai_base_url.includes("localhost:11434") || settings.openai_base_url.includes("127.0.0.1:11434");
		if (!isOllama && !settings.openai_api_key) { setErr("请先填写 API Key"); return; }
		setTesting(true); setTestResult(null); setErr(null);
		try {
			const isGoogle = settings.openai_base_url.includes("googleapis.com");
			let res: Response;
			if (isGoogle) {
				res = await fetch(`${settings.openai_base_url}/models/${settings.model_name}:generateContent?key=${settings.openai_api_key}`, {
					method: "POST", headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ contents: [{ parts: [{ text: "Reply with 'OK' if you received this message." }] }], generationConfig: { temperature: 0.1, maxOutputTokens: 10 } }),
				});
			} else {
				const h: Record<string, string> = { "Content-Type": "application/json" };
				if (!isOllama) h.Authorization = `Bearer ${settings.openai_api_key}`;
				res = await fetch(`${settings.openai_base_url}/chat/completions`, {
					method: "POST", headers: h,
					body: JSON.stringify({ model: settings.model_name, messages: [{ role: "user", content: "Reply with 'OK' if you received this message." }], max_tokens: 10 }),
				});
			}
			if (res.ok) setTestResult({ success: true, message: "连接成功，API 与模型配置可用。" });
			else {
				const e = await res.json().catch(() => ({ error: { message: "未知错误" } }));
				setTestResult({ success: false, message: `连接失败：${e.error?.message || e.message || "请检查 API Key 和 Base URL"}` });
			}
		} catch (e) { setTestResult({ success: false, message: e instanceof Error ? e.message : "测试失败" }); }
		finally { setTesting(false); }
	};

	if (loading) return <div className="lp-loader-center"><Loader2 className="animate-spin" size={28} color="rgb(180,99,60)" /></div>;

	return (
		<div className="lp-stack lp-gap-24 lp-max-860">
			<header>
				<div className="lp-script-sm">Atelier</div>
				<h1 className="lp-page-title">系统 · <em>调音台</em></h1>
				<p className="lp-page-sub">配置翻译服务商、API Key 与模型参数。</p>
			</header>

			{err && <div className="lp-error">{err}</div>}
			{isSaved && <div className="lp-success"><CheckCircle2 size={16} /> 设置已保存</div>}
			{testResult && <div className={testResult.success ? "lp-success" : "lp-error"}><CheckCircle2 size={16} /> {testResult.message}</div>}

			<section className="lp-paper lp-stack lp-gap-14">
				<h2 className="lp-h2">API 服务商</h2>
				<div className="lp-grid lp-grid-preset">
					{API_PRESETS.map((p) => {
						const Icon = p.icon; const active = preset === p.id;
						return (
							<button key={p.id} type="button" onClick={() => pickPreset(p.id)} className={"lp-card-btn " + (active ? "is-active" : "")}>
								<div className="lp-row lp-gap-8 lp-mb-6">
									<Icon size={18} color="rgb(180,99,60)" />
									<span className="lp-preset-title">{p.name}</span>
								</div>
								<p className="lp-muted lp-preset-desc">{p.description}</p>
							</button>
						);
					})}
				</div>
			</section>

			<section className="lp-paper lp-stack lp-gap-18">
				<div>
					<label className="lp-label">API Key</label>
					<div className="lp-field">
						<input className="lp-input lp-input-right" type={showKey ? "text" : "password"} value={settings.openai_api_key} placeholder={data?.apiKeyPlaceholder || "sk-..."} onChange={(e) => setSettings({ ...settings, openai_api_key: e.target.value })} />
						<button type="button" onClick={() => setShowKey(!showKey)} className="lp-field-btn">
							{showKey ? <EyeOff size={16} /> : <Eye size={16} />}
						</button>
					</div>
					<p className="lp-handwrite lp-key-hint">
						{preset === "google" ? "— Google API Key 可在 AI Studio 获取。"
						: preset === "siliconflow" ? "— 硅基流动 Key 可在控制台创建。"
						: preset === "ollama" ? "— Ollama 本地模式通常无需 Key。"
						: "— API Key 将保存在本地数据库中。"}
					</p>
				</div>

				<div>
					<label className="lp-label">API Base URL</label>
					<input className="lp-input" value={settings.openai_base_url} placeholder="https://api.siliconflow.cn/v1" onChange={(e) => setSettings({ ...settings, openai_base_url: e.target.value })} />
					<p className="lp-muted lp-base-hint">支持兼容 OpenAI API 协议的服务。</p>
				</div>

				<div>
					<label className="lp-label">模型名称</label>
					{preset !== "custom" && data?.models ? (
						<div className="lp-stack lp-gap-10">
							<div className="lp-row lp-wrap lp-gap-8">
								{data.models.map((m) => (
									<button key={m} type="button" onClick={() => setSettings({ ...settings, model_name: m })} className={"lp-pill " + (settings.model_name === m ? "is-active" : "")}>{m}</button>
								))}
							</div>
							<input className="lp-input" value={settings.model_name} placeholder="或手动输入模型名称" onChange={(e) => setSettings({ ...settings, model_name: e.target.value })} />
						</div>
					) : (
						<input className="lp-input" value={settings.model_name} placeholder="gpt-4o-mini" onChange={(e) => setSettings({ ...settings, model_name: e.target.value })} />
					)}
				</div>

				<div className="lp-row lp-end lp-gap-10">
					<button className="lp-btn" onClick={() => void test()} disabled={testing}>
						{testing ? <Loader2 size={15} className="animate-spin" /> : <TestTube size={15} />}
						{testing ? "测试中..." : "测试连接"}
					</button>
					<button className="lp-btn lp-btn-primary" onClick={() => void save()} disabled={isSaving}>
						{isSaving ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />}
						{isSaving ? "保存中..." : "保存设置"}
					</button>
				</div>
			</section>
		</div>
	);
}