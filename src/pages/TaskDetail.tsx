import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { ArrowLeft, CheckCircle2, Download, PauseCircle, PlayCircle, RotateCcw, AlertTriangle, Clock3, Loader2 } from "lucide-react";
import { API_BASE_URL, apiGet, apiPostJson } from "../lib/api";
import type { ProjectDetail, RetryTasksResponse, SegmentSummary, TaskControlResponse } from "../lib/types";

const emptySummary: SegmentSummary = { pending: 0, drafting: 0, polishing: 0, completed: 0, qa_failed: 0, failed: 0 };
const fallbackTask: ProjectDetail = { id: "unknown", name: "任务加载中", progress: 0, status: "created", chapters: [], segment_summary: emptySummary };
const emptyQuality = { translated_segments: 0, auto_repaired_segments: 0, qa_failed_segments: 0, high_english_residue_segments: 0, too_short_translation_segments: 0, avg_retry_count: 0 };

function getQualityHealth(q: typeof emptyQuality) {
	const translated = Math.max(1, q.translated_segments);
	const risk = (q.qa_failed_segments + q.high_english_residue_segments + q.too_short_translation_segments) / translated;
	if (q.qa_failed_segments >= 8 || risk >= 0.25) return { label: "需关注", cls: "lp-chip-rose", hint: "建议优先处理质检失败与高风险段落。" };
	if (q.qa_failed_segments >= 3 || risk >= 0.12) return { label: "尚可",   cls: "lp-chip-amber", hint: "整体可用，建议抽样复核关键章节。" };
	return { label: "良好", cls: "lp-chip-green", hint: "质量风险较低，可进入终审。" };
}

const STATUS_CHIP: Record<string, { text: string; cls: string }> = {
	completed:     { text: "已完成",     cls: "lp-chip-green" },
	translating:   { text: "翻译中",     cls: "lp-chip-amber" },
	paused:        { text: "已暂停",     cls: "lp-chip-ink"  },
	pending_terms: { text: "待确认术语", cls: "lp-chip-amber" },
	failed:        { text: "失败",       cls: "lp-chip-rose" },
};

const fillStyle = (pct: number): CSSProperties => ({ width: pct + "%" });

export default function TaskDetail() {
	const { id } = useParams();
	const navigate = useNavigate();
	const projectId = useMemo(() => id || "unknown", [id]);
	const [task, setTask] = useState<ProjectDetail>(fallbackTask);
	const [err, setErr] = useState<string | null>(null);
	const [isDownloading, setIsDownloading] = useState(false);
	const [isRetrying, setIsRetrying] = useState(false);
	const [isPauseToggling, setIsPauseToggling] = useState(false);

	useEffect(() => {
		let timer: number;
		const run = async () => {
			try {
				const detail = await apiGet<ProjectDetail>(`/api/projects/${projectId}/status`);
				setTask(detail); setErr(null);
				if (["parsing", "pending_terms", "translating", "paused", "created"].includes(detail.status)) timer = window.setTimeout(run, 3000);
			} catch (e) {
				const msg = e instanceof Error ? e.message : "加载失败";
				setErr(msg);
				if (!msg.includes("404")) timer = window.setTimeout(run, 5000);
			}
		};
		void run();
		return () => clearTimeout(timer);
	}, [projectId]);

	const parseFilename = (d: string | null): string | null => {
		if (!d) return null;
		const u = d.match(/filename\*=UTF-8''([^;]+)/i);
		if (u?.[1]) { try { return decodeURIComponent(u[1]); } catch { return u[1]; } }
		return d.match(/filename="?([^";]+)"?/i)?.[1] || null;
	};

	const sourceExt = (task.name.match(/\.[^/.]+$/)?.[0] || ".epub").toLowerCase();
	const exportLabel = sourceExt === ".pdf" ? "导出 PDF" : "导出 EPUB";

	const doDownload = async () => {
		setIsDownloading(true);
		try {
			const res = await fetch(`${API_BASE_URL}/api/projects/${projectId}/download`);
			if (!res.ok) throw new Error(`download failed: ${res.status}`);
			const blob = await res.blob();
			const url = URL.createObjectURL(blob);
			const a = document.createElement("a");
			a.href = url;
			const srv = parseFilename(res.headers.get("content-disposition"));
			if (srv) a.download = srv;
			else {
				const mime = (res.headers.get("content-type") || blob.type || "").toLowerCase();
				a.download = `${task.name.replace(/\.[^/.]+$/, "")}_translated${mime.includes("pdf") ? ".pdf" : ".epub"}`;
			}
			document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
			setErr(null);
		} catch (e) { setErr(e instanceof Error ? e.message : "下载失败"); }
		finally { setIsDownloading(false); }
	};

	const doRetry = async () => {
		setIsRetrying(true);
		try {
			const r = await apiPostJson<RetryTasksResponse>("/api/tasks/retry", { project_id: projectId });
			if (r.retried === 0) setErr("当前没有可重试的失败段落"); else setErr(null);
			setTask(await apiGet<ProjectDetail>(`/api/projects/${projectId}/status`));
		} catch (e) { setErr(e instanceof Error ? e.message : "重试失败"); }
		finally { setIsRetrying(false); }
	};

	const doPauseToggle = async () => {
		setIsPauseToggling(true);
		try {
			const path = task.status === "paused" ? "/api/tasks/resume" : "/api/tasks/pause";
			await apiPostJson<TaskControlResponse>(path, { project_id: projectId });
			setTask(await apiGet<ProjectDetail>(`/api/projects/${projectId}/status`));
			setErr(null);
		} catch (e) { setErr(e instanceof Error ? e.message : "状态切换失败"); }
		finally { setIsPauseToggling(false); }
	};

	const failedCount = task.segment_summary.qa_failed + task.segment_summary.failed;
	const quality = task.quality_summary || { ...emptyQuality, translated_segments: task.segment_summary.completed + task.segment_summary.qa_failed, qa_failed_segments: task.segment_summary.qa_failed };
	const health = getQualityHealth(quality);
	const chip = STATUS_CHIP[task.status] || { text: "处理中", cls: "lp-chip-amber" };
	const taskFill = fillStyle(task.progress);

	const kpis: Array<[string, number]> = [
		["排队", task.segment_summary.pending],
		["草译", task.segment_summary.drafting],
		["润色", task.segment_summary.polishing],
		["完成", task.segment_summary.completed],
		["质检失败", task.segment_summary.qa_failed],
		["失败", task.segment_summary.failed],
	];

	const qualityRows: Array<[string, number]> = [
		["已产出译文", quality.translated_segments],
		["自动修复", quality.auto_repaired_segments],
		["待人工复核", quality.qa_failed_segments],
		["英文残留风险", quality.high_english_residue_segments],
		["译文过短风险", quality.too_short_translation_segments],
		["平均重试次数", quality.avg_retry_count],
	];

	return (
		<div className="lp-stack lp-gap-28">
			<button type="button" onClick={() => navigate(-1)} className="lp-btn lp-btn-ghost lp-self-start">
				<ArrowLeft size={16} /> 返回
			</button>

			<header className="lp-row lp-wrap lp-between lp-gap-24">
				<div>
					<div className="lp-script-sm">Chapter in progress</div>
					<h1 className="lp-page-title">{task.name}</h1>
					<p className="lp-page-sub">任务编号 · {projectId}</p>
				</div>
				<span className={"lp-chip lp-chip-lg " + chip.cls}>{chip.text}</span>
			</header>

			<div className="lp-row lp-wrap lp-gap-10">
				{task.status === "pending_terms" && (
					<button className="lp-btn lp-btn-primary" onClick={() => navigate(`/terms?project=${projectId}`)}>去确认术语</button>
				)}
				<button className="lp-btn" onClick={() => void doRetry()} disabled={isRetrying || failedCount === 0}>
					{isRetrying ? <Loader2 size={15} className="animate-spin" /> : <RotateCcw size={15} />}
					重试失败段（{failedCount}）
				</button>
				<button className="lp-btn" onClick={() => void doPauseToggle()} disabled={isPauseToggling || !["translating", "paused"].includes(task.status)}>
					{task.status === "paused" ? <PlayCircle size={15} /> : <PauseCircle size={15} />}
					{isPauseToggling ? "处理中..." : task.status === "paused" ? "继续翻译" : "暂停翻译"}
				</button>
				<button className="lp-btn lp-btn-primary" onClick={() => void doDownload()} disabled={isDownloading || task.status !== "completed"}>
					{isDownloading ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />}
					{isDownloading ? "导出中..." : exportLabel}
				</button>
			</div>

			{err && <div className="lp-error">{err}</div>}

			<section className="lp-paper lp-stack lp-gap-16">
				<div className="lp-row-baseline lp-between">
					<h2 className="lp-h2">翻译进度</h2>
					<span className="lp-numeric lp-numeric-lg">{task.progress}%</span>
				</div>
				<div className="lp-progress lp-progress-lg"><div className="lp-progress-fill" style={taskFill} /></div>
				<div className="lp-grid lp-grid-kpi">
					{kpis.map(([label, v]) => (
						<div key={label} className="lp-kpi">
							<div className="lp-label lp-mb-2">{label}</div>
							<div className="lp-numeric">{v}</div>
						</div>
					))}
				</div>
			</section>

			<section className="lp-card lp-stack lp-gap-12">
				<div className="lp-row lp-gap-8">
					<h3 className="lp-h3">质量健康</h3>
					<span className={"lp-chip " + health.cls}>{health.label}</span>
				</div>
				<p className="lp-handwrite lp-m-0">{health.hint}</p>
				<div className="lp-grid lp-grid-quality">
					{qualityRows.map(([label, v]) => (
						<div key={label} className="lp-kpi-row">
							<span className="lp-muted lp-kpi-label-inline">{label}</span>
							<span className="lp-numeric-sm">{v}</span>
						</div>
					))}
				</div>
			</section>

			<section className="lp-stack lp-gap-12">
				<div className="lp-divider">Chapters</div>
				{task.chapters.length === 0 ? (
					<div className="lp-muted lp-text-center">暂无章节数据</div>
				) : task.chapters.map((c) => {
					const chapFill = fillStyle(c.progress);
					const statusCls = c.status === "completed" ? "lp-chip-green" : c.status === "translating" ? "lp-chip-amber" : c.status === "failed" ? "lp-chip-rose" : "lp-chip-ink";
					return (
						<div key={c.id} className="lp-card lp-row lp-gap-16">
							<div className="lp-flex1">
								<div className="lp-label">CHAPTER · {c.id}</div>
								<div className="lp-chapter-title">{c.title}</div>
							</div>
							<div className="lp-chapter-progress">
								<div className="lp-progress"><div className="lp-progress-fill" style={chapFill} /></div>
								<span className="lp-muted lp-fs-11 lp-text-right">{c.progress}%</span>
							</div>
							<span className={"lp-chip " + statusCls}>
								{c.status === "completed" ? <><CheckCircle2 size={12} /> 完成</> : c.status === "translating" ? <><Loader2 size={12} className="animate-spin" /> 翻译中</> : c.status === "failed" ? <><AlertTriangle size={12} /> 失败</> : <><Clock3 size={12} /> 排队</>}
							</span>
						</div>
					);
				})}
			</section>
		</div>
	);
}