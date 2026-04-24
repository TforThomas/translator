import { useEffect, useMemo, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  CheckCircle2,
  Download,
  PauseCircle,
  PlayCircle,
  RotateCcw,
  AlertTriangle,
  Clock3,
  Loader2,
} from "lucide-react";
import { API_BASE_URL, apiGet, apiPostJson } from "../lib/api";
import type { ProjectDetail, RetryTasksResponse, SegmentSummary, TaskControlResponse } from "../lib/types";

const emptySummary: SegmentSummary = {
  pending: 0,
  drafting: 0,
  polishing: 0,
  completed: 0,
  qa_failed: 0,
  failed: 0,
};

const fallbackTask: ProjectDetail = {
  id: "unknown",
  name: "任务加载中",
  progress: 0,
  status: "created",
  chapters: [],
  segment_summary: emptySummary,
};

const emptyQualitySummary = {
  translated_segments: 0,
  auto_repaired_segments: 0,
  qa_failed_segments: 0,
  high_english_residue_segments: 0,
  too_short_translation_segments: 0,
  avg_retry_count: 0,
};

function getQualityHealth(qualitySummary: typeof emptyQualitySummary) {
  const translated = Math.max(1, qualitySummary.translated_segments);
  const riskCount =
    qualitySummary.qa_failed_segments +
    qualitySummary.high_english_residue_segments +
    qualitySummary.too_short_translation_segments;
  const riskRatio = riskCount / translated;

  if (qualitySummary.qa_failed_segments >= 8 || riskRatio >= 0.25) {
    return {
      label: "需关注",
      className: "bg-rose-50 text-rose-700 border-rose-200",
      hint: "建议优先处理质检失败与高风险段落。",
    };
  }

  if (qualitySummary.qa_failed_segments >= 3 || riskRatio >= 0.12) {
    return {
      label: "一般",
      className: "bg-amber-50 text-amber-700 border-amber-200",
      hint: "整体可用，建议抽样复核关键章节。",
    };
  }

  return {
    label: "良好",
    className: "bg-emerald-50 text-emerald-700 border-emerald-200",
    hint: "质量风险较低，可优先进入终审。",
  };
}

export default function TaskDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const projectId = useMemo(() => id || "unknown", [id]);
  const [task, setTask] = useState<ProjectDetail>(fallbackTask);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isDownloading, setIsDownloading] = useState(false);
  const [isRetrying, setIsRetrying] = useState(false);
  const [isTogglingPause, setIsTogglingPause] = useState(false);

  useEffect(() => {
    let timer: number;
    const run = async () => {
      try {
        const detail = await apiGet<ProjectDetail>(`/api/projects/${projectId}/status`);
        setTask(detail);
        setLoadError(null);
        if (["parsing", "pending_terms", "translating", "paused", "created"].includes(detail.status)) {
          timer = window.setTimeout(run, 3000);
        }
      } catch (e) {
        const message = e instanceof Error ? e.message : "加载失败";
        setLoadError(message);
        if (message.includes("404")) {
          return;
        }
        timer = window.setTimeout(run, 5000);
      }
    };
    void run();
    return () => clearTimeout(timer);
  }, [projectId]);

  const parseFilenameFromDisposition = (disposition: string | null): string | null => {
    if (!disposition) return null;
    const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf8Match?.[1]) {
      try {
        return decodeURIComponent(utf8Match[1]);
      } catch {
        return utf8Match[1];
      }
    }
    const normalMatch = disposition.match(/filename="?([^";]+)"?/i);
    return normalMatch?.[1] || null;
  };

  const sourceExt = (task.name.match(/\.[^/.]+$/)?.[0] || ".epub").toLowerCase();
  const exportLabel = sourceExt === ".pdf" ? "导出译文 PDF" : "导出译文 EPUB";

  const downloadEpub = async () => {
    setIsDownloading(true);
    try {
      const res = await fetch(`${API_BASE_URL}/api/projects/${projectId}/download`);
      if (!res.ok) throw new Error(`download failed: ${res.status}`);

      const expectedFormat = sourceExt === ".pdf" ? "pdf" : "epub";
      const exportFormat = (res.headers.get("x-export-format") || "").toLowerCase();
      if (exportFormat && exportFormat !== expectedFormat) {
        throw new Error(`导出格式异常：期望 ${expectedFormat.toUpperCase()}，实际 ${exportFormat.toUpperCase()}`);
      }

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const serverFilename = parseFilenameFromDisposition(res.headers.get("content-disposition"));
      if (serverFilename) {
        a.download = serverFilename;
      } else {
        const mime = (res.headers.get("content-type") || blob.type || "").toLowerCase();
        const outputExt = mime.includes("pdf") ? ".pdf" : ".epub";
        a.download = `${task.name.replace(/\.[^/.]+$/, "")}_translated${outputExt}`;
      }
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "下载失败");
    } finally {
      setIsDownloading(false);
    }
  };

  const handleRetryFailed = async () => {
    setIsRetrying(true);
    try {
      const result = await apiPostJson<RetryTasksResponse, { project_id: string }>("/api/tasks/retry", {
        project_id: projectId,
      });
      if (result.retried === 0) {
        setLoadError("当前没有可重试的失败段落");
      } else {
        setLoadError(null);
      }
      const detail = await apiGet<ProjectDetail>(`/api/projects/${projectId}/status`);
      setTask(detail);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "重试失败");
    } finally {
      setIsRetrying(false);
    }
  };

  const handleTogglePause = async () => {
    setIsTogglingPause(true);
    try {
      const path = task.status === "paused" ? "/api/tasks/resume" : "/api/tasks/pause";
      await apiPostJson<TaskControlResponse, { project_id: string }>(path, {
        project_id: projectId,
      });
      const detail = await apiGet<ProjectDetail>(`/api/projects/${projectId}/status`);
      setTask(detail);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "任务状态切换失败");
    } finally {
      setIsTogglingPause(false);
    }
  };

  const failedCount = task.segment_summary.qa_failed + task.segment_summary.failed;
  const qualitySummary = task.quality_summary || {
    ...emptyQualitySummary,
    translated_segments: task.segment_summary.completed + task.segment_summary.qa_failed,
    qa_failed_segments: task.segment_summary.qa_failed,
  };
  const qualityHealth = getQualityHealth(qualitySummary);

  const statusChip = (() => {
    if (task.status === "completed") {
      return <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-semibold text-emerald-700"><CheckCircle2 size={14} />已完成</span>;
    }
    if (task.status === "translating") {
      return <span className="inline-flex items-center gap-1 rounded-full bg-blue-50 px-2.5 py-1 text-xs font-semibold text-blue-700"><Loader2 size={14} className="animate-spin" />翻译中</span>;
    }
    if (task.status === "paused") {
      return <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-700"><PauseCircle size={14} />已暂停</span>;
    }
    if (task.status === "pending_terms") {
      return <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2.5 py-1 text-xs font-semibold text-amber-700"><AlertTriangle size={14} />待确认术语</span>;
    }
    if (task.status === "failed") {
      return <span className="inline-flex items-center gap-1 rounded-full bg-rose-50 px-2.5 py-1 text-xs font-semibold text-rose-700"><AlertTriangle size={14} />失败</span>;
    }
    return <span className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2.5 py-1 text-xs font-semibold text-slate-700"><Clock3 size={14} />处理中</span>;
  })();

  return (
    <div className="space-y-6">
      <section className="page-header">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="space-y-3">
            <button onClick={() => navigate(-1)} className="btn-secondary h-9 rounded-lg px-3">
              <ArrowLeft size={16} />
              返回
            </button>
            <div>
              <h1 className="text-2xl font-semibold tracking-tight text-slate-900">{task.name}</h1>
              <p className="mt-1 text-xs text-slate-500">任务 ID：{projectId}</p>
            </div>
            {statusChip}
          </div>

          <div className="flex flex-wrap items-center gap-2 lg:justify-end">
            {task.status === "pending_terms" && (
              <button className="btn-primary rounded-lg" onClick={() => navigate(`/terms?project=${projectId}`)}>
                去确认术语
              </button>
            )}
            <button
              className="btn-secondary rounded-lg"
              onClick={() => void handleRetryFailed()}
              disabled={isRetrying || failedCount === 0}
              aria-disabled={isRetrying || failedCount === 0}
            >
              {isRetrying ? <Loader2 size={16} className="animate-spin" /> : <RotateCcw size={16} />}
              重试失败段落（{failedCount}）
            </button>
            <button
              className="btn-secondary rounded-lg"
              onClick={() => void handleTogglePause()}
              disabled={isTogglingPause || !["translating", "paused"].includes(task.status)}
              aria-disabled={isTogglingPause || !["translating", "paused"].includes(task.status)}
            >
              {task.status === "paused" ? <PlayCircle size={16} /> : <PauseCircle size={16} />}
              {isTogglingPause ? "处理中..." : task.status === "paused" ? "继续翻译" : "暂停翻译"}
            </button>
            <button
              className="inline-flex h-10 items-center gap-2 rounded-lg bg-emerald-600 px-4 text-sm font-medium text-white transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => void downloadEpub()}
              disabled={isDownloading || task.status !== "completed"}
              aria-disabled={isDownloading || task.status !== "completed"}
            >
              {isDownloading ? <Loader2 size={16} className="animate-spin" /> : <Download size={16} />}
              {isDownloading ? "正在导出..." : exportLabel}
            </button>
          </div>
        </div>
      </section>

      {loadError && <div className="alert-danger">{loadError}</div>}

      <section className="grid gap-4 lg:grid-cols-3">
        <div className="panel p-6 lg:col-span-2">
          <div className="mb-5 flex items-end justify-between">
            <div>
              <p className="text-sm text-slate-500">总进度</p>
              <p className="mt-1 text-4xl font-semibold text-slate-900">{task.progress}%</p>
            </div>
          </div>

          <div className="h-3 rounded-full bg-slate-100">
            <div className="h-3 rounded-full bg-[rgb(var(--primary))] transition-all" style={{ width: `${task.progress}%` }} />
          </div>

          <div className="mt-5 grid grid-cols-2 gap-2 lg:grid-cols-3">
            <div className="rounded-xl bg-slate-50 px-3 py-2 text-xs text-slate-700">排队中 {task.segment_summary.pending}</div>
            <div className="rounded-xl bg-blue-50 px-3 py-2 text-xs text-blue-700">草译中 {task.segment_summary.drafting}</div>
            <div className="rounded-xl bg-indigo-50 px-3 py-2 text-xs text-indigo-700">润色中 {task.segment_summary.polishing}</div>
            <div className="rounded-xl bg-emerald-50 px-3 py-2 text-xs text-emerald-700">已完成 {task.segment_summary.completed}</div>
            <div className="rounded-xl bg-amber-50 px-3 py-2 text-xs text-amber-700">质检失败 {task.segment_summary.qa_failed}</div>
            <div className="rounded-xl bg-rose-50 px-3 py-2 text-xs text-rose-700">失败 {task.segment_summary.failed}</div>
          </div>
        </div>

        <div className="panel p-6">
          <div className="mb-3 flex items-center justify-between">
            <p className="text-sm font-semibold text-slate-900">质量健康</p>
            <span className={`rounded-full border px-2.5 py-1 text-xs font-semibold ${qualityHealth.className}`}>{qualityHealth.label}</span>
          </div>
          <p className="text-xs text-slate-500">{qualityHealth.hint}</p>

          <div className="mt-4 space-y-2 text-xs">
            <div className="panel-muted px-3 py-2 text-slate-700">已产出译文 {qualitySummary.translated_segments}</div>
            <div className="panel-muted px-3 py-2 text-emerald-700">自动修复成功 {qualitySummary.auto_repaired_segments}</div>
            <div className="panel-muted px-3 py-2 text-amber-700">待人工复核 {qualitySummary.qa_failed_segments}</div>
            <div className="panel-muted px-3 py-2 text-rose-700">英文残留风险 {qualitySummary.high_english_residue_segments}</div>
            <div className="panel-muted px-3 py-2 text-orange-700">译文过短风险 {qualitySummary.too_short_translation_segments}</div>
            <div className="panel-muted px-3 py-2 text-slate-700">平均重试次数 {qualitySummary.avg_retry_count}</div>
          </div>
        </div>
      </section>

      <section className="panel overflow-hidden">
        <div className="border-b border-[rgb(var(--line))] px-6 py-4">
          <h2 className="text-base font-semibold text-slate-900">章节翻译状态</h2>
        </div>

        <div className="divide-y divide-[rgb(var(--line))]">
          {task.chapters.map((chapter) => (
            <div key={chapter.id} className="px-6 py-4">
              <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
                <div className="min-w-0">
                  <p className="text-xs text-slate-500">章节 {chapter.id}</p>
                  <p className="truncate text-sm font-semibold text-slate-900">{chapter.title}</p>
                </div>

                <div className="flex items-center gap-4 lg:min-w-[340px] lg:justify-end">
                  <div className="w-full max-w-52">
                    <div className="mb-1 text-right text-xs text-slate-500">{chapter.progress}%</div>
                    <div className="h-2 rounded-full bg-slate-100">
                      <div
                        className={`h-2 rounded-full ${chapter.status === "completed" ? "bg-emerald-600" : chapter.status === "failed" ? "bg-rose-600" : "bg-[rgb(var(--primary))]"}`}
                        style={{ width: `${chapter.progress}%` }}
                      />
                    </div>
                  </div>

                  <div className="w-20 text-right text-xs font-semibold">
                    {chapter.status === "completed" && <span className="text-emerald-700">完成</span>}
                    {chapter.status === "translating" && <span className="text-blue-700">翻译中</span>}
                    {chapter.status === "pending" && <span className="text-slate-600">排队中</span>}
                    {chapter.status === "failed" && <span className="text-rose-700">失败</span>}
                  </div>
                </div>
              </div>
            </div>
          ))}

          {task.chapters.length === 0 && <div className="px-6 py-10 text-center text-sm text-slate-500">暂无章节数据</div>}
        </div>
      </section>
    </div>
  );
}