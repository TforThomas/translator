import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useNavigate } from "react-router-dom";
import { Upload, Send, FileText, Loader2, Trash2, Sparkles, ArrowLeftRight, ChevronRight } from "lucide-react";
import { apiGet, apiPostJson, apiPostFile, apiDelete } from "../lib/api";
import type { ProjectSummary } from "../lib/types";

const LANGS = [
	{ code: "en", label: "English" },
	{ code: "zh", label: "中文" },
	{ code: "ja", label: "日本語" },
	{ code: "fr", label: "Français" },
	{ code: "de", label: "Deutsch" },
	{ code: "es", label: "Español" },
];

const STATUS_LABEL: Record<string, { text: string; cls: string }> = {
	created:       { text: "筹备中", cls: "lp-chip-ink" },
	uploading:     { text: "上传中", cls: "lp-chip-amber" },
	parsing:       { text: "解析中", cls: "lp-chip-amber" },
	pending_terms: { text: "待确认", cls: "lp-chip-amber" },
	translating:   { text: "翻译中", cls: "lp-chip-amber" },
	paused:        { text: "已暂停", cls: "lp-chip-ink" },
	completed:     { text: "已完成", cls: "lp-chip-green" },
	failed:        { text: "失败",   cls: "lp-chip-rose" },
};

const fillStyle = (pct: number): CSSProperties => ({ width: pct + "%" });

export default function Home() {
	const navigate = useNavigate();
	const fileInput = useRef<HTMLInputElement>(null);
	const [projects, setProjects] = useState<ProjectSummary[]>([]);
	const [source, setSource] = useState("en");
	const [target, setTarget] = useState("zh");
	const [enableOcr, setEnableOcr] = useState(false);
	const [uploading, setUploading] = useState(false);
	const [err, setErr] = useState<string | null>(null);

	useEffect(() => {
		let timer: number;
		const run = async () => {
			try { setProjects(await apiGet<ProjectSummary[]>("/api/projects")); }
			catch (e) { console.error(e); }
			timer = window.setTimeout(run, 3000);
		};
		void run();
		return () => clearTimeout(timer);
	}, []);

	const handleFile = async (file: File) => {
		setErr(null);
		if (!/\.(epub|pdf)$/i.test(file.name)) { setErr("仅支持 EPUB 或 PDF 文件"); return; }
		if (file.size > 100 * 1024 * 1024) { setErr("文件不得超过 100MB"); return; }
		setUploading(true);
		try {
			const created = await apiPostJson<{ id: string }>("/api/projects", {
				name: file.name,
				source_lang: source,
				target_lang: target,
				enable_ocr: enableOcr,
			});
			await apiPostFile(`/api/projects/${created.id}/upload`, file);
			navigate(`/task/${created.id}`);
		} catch (e) {
			setErr(e instanceof Error ? e.message : "上传失败");
		} finally {
			setUploading(false);
		}
	};

	const deleteProject = async (id: string, e: React.MouseEvent) => {
		e.stopPropagation();
		if (!confirm("确认删除这个翻译任务？")) return;
		try { await apiDelete(`/api/projects/${id}`); setProjects((prev) => prev.filter((p) => p.id !== id)); }
		catch (err) { console.error(err); }
	};

	return (
		<div className="lp-stack lp-gap-36">
			<header>
				<div className="lp-script">Good evening,</div>
				<h1 className="lp-page-title">把一本书，<em>译成心声</em></h1>
				<p className="lp-page-sub">拖一本 EPUB 或 PDF 进来，我帮你慢慢翻。</p>
			</header>

			<section className="lp-paper lp-paper-torn lp-stack lp-gap-18">
				<div className="lp-row lp-between">
					<h2 className="lp-h2">今日新作</h2>
					<span className="lp-handwrite">— 自在翻译，不设期限</span>
				</div>

				<div className="lp-row lp-wrap lp-gap-10">
					<span className="lp-muted lp-fs-13 lp-mr-4">原文</span>
					{LANGS.map((l) => (
						<button key={"s-" + l.code} type="button" className={"lp-pill " + (source === l.code ? "is-active" : "")} onClick={() => setSource(l.code)}>{l.label}</button>
					))}
					<ArrowLeftRight size={16} className="lp-arrow-icon" />
					<span className="lp-muted lp-fs-13 lp-mr-4">译文</span>
					{LANGS.map((l) => (
						<button key={"t-" + l.code} type="button" className={"lp-pill " + (target === l.code ? "is-active" : "")} onClick={() => setTarget(l.code)}>{l.label}</button>
					))}
				</div>

				<label
					className="lp-upload"
					onDragOver={(e) => e.preventDefault()}
					onDrop={(e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) void handleFile(f); }}
				>
					<input
						ref={fileInput}
						type="file"
						accept=".epub,.pdf"
						className="lp-hidden"
						onChange={(e) => { const f = e.target.files?.[0]; if (f) void handleFile(f); }}
					/>
					{uploading ? <Loader2 className="animate-spin" size={28} color="rgb(180,99,60)" /> : <Upload size={28} color="rgb(180,99,60)" />}
					<div className="lp-upload-main">
						{uploading ? "正在上传..." : "拖拽 EPUB / PDF 到此，或 "}
						{!uploading && <span className="lp-script-inline">点此选择</span>}
					</div>
					<div className="lp-upload-hint">最大 100MB · 支持扫描 PDF（请开启 OCR）</div>
				</label>

				<div className="lp-ai-row">
					<Sparkles size={18} color="rgb(196,154,67)" />
					<span className="lp-ai-label">AI 启发（扫描件 OCR）</span>
					<label className="lp-toggle">
						<input type="checkbox" checked={enableOcr} onChange={(e) => setEnableOcr(e.target.checked)} />
						<span className="lp-toggle-track"><span className="lp-toggle-thumb" /></span>
					</label>
				</div>

				<div className="lp-row lp-end lp-gap-14">
					<span className="lp-handwrite">— 一切准备就绪</span>
					<button type="button" className="lp-btn lp-btn-primary" onClick={() => fileInput.current?.click()} disabled={uploading}>
						<Send size={16} /> 开始翻译
					</button>
				</div>

				{err && <div className="lp-error">{err}</div>}
			</section>

			<section className="lp-stack lp-gap-14">
				<div className="lp-divider">Your Translations</div>
				{projects.length === 0 ? (
					<div className="lp-card lp-empty-state">尚无作品，上传一本书开启你的第一次翻译之旅。</div>
				) : (
					<div className="lp-grid lp-grid-cards">
						{projects.map((p) => {
							const chip = STATUS_LABEL[p.status] || STATUS_LABEL.created;
							const fill = fillStyle(p.progress);
							return (
								<article key={p.id} className="lp-card lp-card-project" onClick={() => navigate(`/task/${p.id}`)}>
									<div className="lp-row-start lp-gap-10">
										<FileText size={22} color="rgb(180,99,60)" />
										<div className="lp-flex1">
											<div className="lp-file-name">{p.name}</div>
											<div className="lp-muted lp-project-meta">
												{new Date(p.created_at).toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" })}
											</div>
										</div>
										<button type="button" onClick={(e) => void deleteProject(p.id, e)} className="lp-btn lp-btn-ghost lp-btn-icon" aria-label="删除"><Trash2 size={14} /></button>
									</div>
									<div className="lp-progress"><div className="lp-progress-fill" style={fill} /></div>
									<div className="lp-row lp-between">
										<span className={"lp-chip " + chip.cls}>{chip.text}</span>
										<span className="lp-muted lp-pct-pill">{p.progress}% <ChevronRight size={14} /></span>
									</div>
								</article>
							);
						})}
					</div>
				)}
			</section>
		</div>
	);
}