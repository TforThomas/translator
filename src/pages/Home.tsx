import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Upload,
  FileText,
  ArrowRight,
  CheckCircle2,
  Loader2,
  Trash2,
  PauseCircle,
  AlertTriangle,
  Clock3,
} from "lucide-react";
import { apiDelete, apiGet, apiPostFile, apiPostJson } from "../lib/api";
import type { ProjectSummary, ProjectStatus } from "../lib/types";
import { useUIMode } from "../hooks/useUiMode";

function formatDate(iso: string) {
  const d = new Date(iso);
  const yyyy = d.getFullYear();
  const mm = `${d.getMonth() + 1}`.padStart(2, "0");
  const dd = `${d.getDate()}`.padStart(2, "0");
  const hh = `${d.getHours()}`.padStart(2, "0");
  const mi = `${d.getMinutes()}`.padStart(2, "0");
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}`;
}

const statusMeta: Record<
  ProjectStatus,
  {
    label: string;
    className: string;
    Icon: typeof Loader2;
  }
> = {
  created: {
    label: "准备中",
    className: "bg-slate-100 text-slate-700",
    Icon: Clock3,
  },
  uploading: {
    label: "上传中",
    className: "bg-slate-100 text-slate-700",
    Icon: Loader2,
  },
  parsing: {
    label: "解析中",
    className: "bg-slate-100 text-slate-700",
    Icon: Loader2,
  },
  pending_terms: {
    label: "待确认术语",
    className: "bg-amber-50 text-amber-700",
    Icon: AlertTriangle,
  },
  translating: {
    label: "翻译中",
    className: "bg-blue-50 text-blue-700",
    Icon: Loader2,
  },
  paused: {
    label: "已暂停",
    className: "bg-slate-100 text-slate-700",
    Icon: PauseCircle,
  },
  completed: {
    label: "已完成",
    className: "bg-emerald-50 text-emerald-700",
    Icon: CheckCircle2,
  },
  failed: {
    label: "失败",
    className: "bg-rose-50 text-rose-700",
    Icon: AlertTriangle,
  },
};

export default function Home() {
  const navigate = useNavigate();
  const { isArtMode } = useUIMode();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [useOCR, setUseOCR] = useState(false);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [isCreating, setIsCreating] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refreshProjects = async () => {
    try {
      const list = await apiGet<ProjectSummary[]>("/api/projects");
      setProjects(list);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "加载失败");
    }
  };

  useEffect(() => {
    void refreshProjects();
    const timer = setInterval(refreshProjects, 3000);
    return () => clearInterval(timer);
  }, []);

  const stats = useMemo(() => {
    const total = projects.length;
    const running = projects.filter((p) => p.status === "translating").length;
    const waiting = projects.filter((p) => p.status === "pending_terms").length;
    const done = projects.filter((p) => p.status === "completed").length;
    return { total, running, waiting, done };
  }, [projects]);

  const handleDeleteProject = async (e: React.MouseEvent, projectId: string, projectName: string) => {
    e.stopPropagation();
    if (!confirm(`确定要删除任务 "${projectName}" 吗？此操作不可撤销。`)) {
      return;
    }
    try {
      await apiDelete<{ ok: true }>(`/api/projects/${projectId}`);
      await refreshProjects();
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "删除任务失败");
    }
  };

  const openFilePicker = () => {
    fileInputRef.current?.click();
  };

  const handleSelectedFile = async (file: File) => {
    setIsCreating(true);
    setLoadError(null);
    try {
      const created = await apiPostJson<
        { id: string; name: string; status: string; created_at: string },
        { name: string; source_lang: string; target_lang: string; enable_ocr: boolean }
      >("/api/projects", {
        name: file.name,
        source_lang: "en",
        target_lang: "zh",
        enable_ocr: useOCR,
      });

      await apiPostFile<{ ok: true }>(`/api/projects/${created.id}/upload`, file);
      await refreshProjects();
      navigate(`/task/${created.id}`);
    } catch (e) {
      const errorMsg = e instanceof Error ? e.message : "创建任务失败";
      setLoadError(errorMsg);
      console.error("Upload error:", e);
    } finally {
      setIsCreating(false);
    }
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = () => {
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) {
      void handleSelectedFile(file);
    }
  };

  const featuredProjects = projects.slice(0, 3);

  if (isArtMode) {
    return (
      <div className="art-home art-scene relative min-h-[900px] overflow-hidden rounded-[28px] border border-[rgb(var(--line))]/70 p-0">
        <input
          ref={fileInputRef}
          type="file"
          className="hidden"
          accept=".pdf,.epub,application/pdf,application/epub+zip"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) {
              void handleSelectedFile(file);
            }
            e.currentTarget.value = "";
          }}
        />

        <p className="art-brand absolute left-10 top-10 text-[38px]">LingoPoet</p>

        <div className="art-greeting absolute right-8 top-9 hidden items-center gap-3 rounded-2xl border border-[rgb(var(--line))]/70 px-4 py-2 text-right backdrop-blur-sm lg:flex">
          <div>
            <p className="text-xs text-[rgb(var(--text-muted))]">Good evening, Aria</p>
            <p className="text-sm text-[rgb(var(--text))]">今天想用什么感语言，创造什么故事？</p>
          </div>
          <div className="art-avatar flex h-10 w-10 items-center justify-center rounded-full border border-[rgb(var(--line))] text-sm">A</div>
        </div>

        <section className="art-left-copy absolute left-[130px] top-[190px] max-w-[560px]">
          <h1 className="text-[78px] font-semibold leading-[0.98] tracking-tight text-[rgb(var(--text))]">Translate,<br />like an <span className="art-script text-[rgb(var(--primary))]">Artist.</span></h1>
          <p className="mt-8 max-w-md text-[28px] leading-[1.4] text-[rgb(var(--text-muted))]">翻译，不只是语言的转换，<br />而是把理解，化作另一种美的表达。</p>
          <button className="btn-primary mt-9 h-14 rounded-full px-9 text-lg" onClick={openFilePicker}>
            开始创作
            <ArrowRight size={18} />
          </button>
        </section>

        <section className="art-mode-note absolute left-[980px] top-[145px] hidden rounded-2xl border border-[rgb(var(--line))]/70 px-4 py-3 text-sm lg:flex lg:items-center lg:gap-3">
          <span className="font-semibold text-[rgb(var(--text))]">AI 意境模式</span>
          <label className="relative inline-flex h-6 w-11 items-center">
            <input
              type="checkbox"
              className="peer sr-only"
              checked={useOCR}
              onChange={(e) => setUseOCR(e.target.checked)}
            />
            <span className="h-6 w-11 rounded-full bg-[rgb(var(--surface-muted))] transition peer-checked:bg-[rgb(var(--primary))]" />
            <span className="absolute left-1 top-1 h-4 w-4 rounded-full bg-white transition peer-checked:translate-x-5" />
          </label>
        </section>

        <section
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          onClick={openFilePicker}
          className={`art-paper art-paper-panel absolute right-[110px] top-[270px] w-[640px] cursor-pointer rounded-[28px] border px-10 py-10 transition ${
            isDragging ? "ring-2 ring-[rgb(var(--primary))]" : ""
          }`}
        >
          <p className="art-paper-text text-center text-[28px]">输入想翻译的文字，或从灵感库中选择</p>
          <p className="art-paper-placeholder mt-8 text-center text-[76px] italic">Type something...</p>

          <div className="art-paper-text mt-12 flex items-center justify-between gap-3 text-[22px]">
            <span className="art-pill rounded-full border px-5 py-2.5">自动侦测</span>
            <span className="art-pill rounded-full border px-5 py-2.5">繁體中文</span>
            <button
              className="art-send-btn inline-flex h-16 w-16 items-center justify-center rounded-full transition"
              onClick={(e) => {
                e.stopPropagation();
                openFilePicker();
              }}
            >
              <ArrowRight size={24} />
            </button>
          </div>
        </section>

        <section className="art-cards-wrap absolute bottom-[85px] left-[95px] hidden lg:block">
          <div className="mb-3 flex items-center justify-between">
            <p className="text-xl font-semibold text-[rgb(var(--text))]">灵感卡片</p>
            <p className="text-sm text-[rgb(var(--text-muted))]">每日更新</p>
          </div>
          <div className="flex gap-4">
            {featuredProjects.length > 0 ? (
              featuredProjects.map((project, index) => (
                <button
                  key={project.id}
                  onClick={() => navigate(`/task/${project.id}`)}
                  className={`art-story-card ${index === 1 ? "art-story-card-active" : ""}`}
                >
                  <p className="line-clamp-2 text-left text-base font-semibold">{project.name.replace(/\.[^/.]+$/, "")}</p>
                  <p className="mt-2 text-left text-xs text-[rgb(var(--text-muted))]">{project.status === "completed" ? "散文诗" : "叙事"}</p>
                  <div className="mt-4 flex items-center justify-between text-xs text-[rgb(var(--text-muted))]">
                    <span>{project.progress}%</span>
                    <span>{project.status === "completed" ? "♡ 328" : "进行中"}</span>
                  </div>
                </button>
              ))
            ) : (
              <>
                <div className="art-story-card">
                  <p className="text-base font-semibold">回廊海的对话</p>
                  <p className="mt-2 text-xs text-[rgb(var(--text-muted))]">日 → 中</p>
                </div>
                <div className="art-story-card art-story-card-active">
                  <p className="text-base font-semibold">夏日午后的告白</p>
                  <p className="mt-2 text-xs text-[rgb(var(--text-muted))]">日 → 中</p>
                </div>
                <div className="art-story-card">
                  <p className="text-base font-semibold">宇宙夜的絮语</p>
                  <p className="mt-2 text-xs text-[rgb(var(--text-muted))]">英 → 中</p>
                </div>
              </>
            )}
          </div>
        </section>

        <section className="art-features-wrap absolute bottom-[120px] right-[95px] hidden w-[650px] grid-cols-3 gap-4 lg:grid">
          <div className="art-feature">专注模式<br /><span>沉浸翻译</span></div>
          <div className="art-feature">风格调色盘<br /><span>选择表达风格</span></div>
          <div className="art-feature">我的字典<br /><span>你的专属用语库</span></div>
        </section>

        <button className="btn-primary art-mobile-upload fixed bottom-5 right-5 z-20 h-12 rounded-full px-5 text-sm lg:hidden" onClick={openFilePicker}>
          上传文件
          <ArrowRight size={16} />
        </button>

        <div className="sr-only">
          <h2>近期任务</h2>
          {projects.map((project) => (
            <p key={project.id}>{project.name}</p>
          ))}
          {loadError && <p>{loadError}</p>}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <section className={`page-header ${isArtMode ? "art-hero" : ""}`}>
        <div className="flex w-full flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
          <div>
            {isArtMode ? (
              <>
                <h1 className="text-4xl font-semibold tracking-tight text-[rgb(var(--text))] lg:text-5xl">Translate, like an Artist.</h1>
                <p className="mt-3 max-w-xl text-sm text-[rgb(var(--text-muted))]">翻译不只是替换词句，而是让语义在另一种语言里保持温度、节奏与风格。</p>
                <button className="btn-primary mt-5 px-6" onClick={openFilePicker}>
                  开始创作
                  <ArrowRight size={16} />
                </button>
              </>
            ) : (
              <>
                <h1 className="text-3xl font-semibold tracking-tight text-slate-900">翻译工作台</h1>
                <p className="mt-2 text-sm text-slate-600">上传长文本，持续跟踪翻译状态，完成后保持原格式导出。</p>
              </>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <div className="panel-muted px-4 py-3">
              <p className="text-xs text-slate-500">任务总数</p>
              <p className="mt-1 text-xl font-semibold text-slate-900">{stats.total}</p>
            </div>
            <div className="panel-muted px-4 py-3">
              <p className="text-xs text-slate-500">翻译中</p>
              <p className="mt-1 text-xl font-semibold text-blue-700">{stats.running}</p>
            </div>
            <div className="panel-muted px-4 py-3">
              <p className="text-xs text-slate-500">待确认术语</p>
              <p className="mt-1 text-xl font-semibold text-amber-700">{stats.waiting}</p>
            </div>
            <div className="panel-muted px-4 py-3">
              <p className="text-xs text-slate-500">已完成</p>
              <p className="mt-1 text-xl font-semibold text-emerald-700">{stats.done}</p>
            </div>
          </div>
        </div>
      </section>

      <section className={`panel p-6 md:p-7 ${isArtMode ? "relative overflow-hidden" : ""}`}>
        <input
          ref={fileInputRef}
          type="file"
          className="hidden"
          accept=".pdf,.epub,application/pdf,application/epub+zip"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) {
              void handleSelectedFile(file);
            }
            e.currentTarget.value = "";
          }}
        />

        <div
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          onClick={openFilePicker}
          className={`rounded-2xl border-2 border-dashed px-6 py-12 text-center transition ${
            isDragging
              ? "border-[rgb(var(--primary))] bg-[rgb(var(--primary-soft))]"
              : isArtMode
              ? "art-paper border-[rgb(var(--line))] hover:border-[rgb(var(--primary))]"
              : "border-[rgb(var(--line))] bg-[rgb(var(--surface-muted))] hover:border-[rgb(var(--primary))]"
          }`}
        >
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-white text-[rgb(var(--primary))]">
            <Upload size={20} />
          </div>
          <h3 className={`mt-4 text-lg font-semibold ${isArtMode ? "text-[rgb(var(--text))]" : "text-slate-900"}`}>拖拽文件到这里，或点击选择文件</h3>
          <p className={`mt-2 text-sm ${isArtMode ? "text-[rgb(var(--text-muted))]" : "text-slate-500"}`}>支持 PDF、EPUB 格式，建议单次上传一个文件以便追踪进度。</p>
        </div>

        <div className="mt-5 flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <label className={`flex cursor-pointer items-center gap-3 text-sm ${isArtMode ? "text-[rgb(var(--text-muted))]" : "text-slate-700"}`}>
            <input type="checkbox" className="h-4 w-4 rounded border-slate-300" checked={useOCR} onChange={(e) => setUseOCR(e.target.checked)} />
            扫描页启用 OCR（适用于图片型 PDF）
          </label>
          <button
            className="btn-primary px-5"
            onClick={openFilePicker}
            disabled={isCreating}
            aria-disabled={isCreating}
          >
            {isCreating ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                正在创建任务
              </>
            ) : (
              <>
                开始解析
                <ArrowRight size={16} />
              </>
            )}
          </button>
        </div>
      </section>

      <section className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="section-title">近期任务</h2>
          <p className="text-xs text-slate-500">每 3 秒自动刷新</p>
        </div>

        {loadError && <div className="alert-danger">{loadError}</div>}

        <div className="space-y-3">
          {projects.map((project) => {
            const meta = statusMeta[project.status];
            const spinning = project.status === "translating" || project.status === "uploading" || project.status === "parsing";
            return (
              <div
                key={project.id}
                onClick={() => navigate(`/task/${project.id}`)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    navigate(`/task/${project.id}`);
                  }
                }}
                role="button"
                tabIndex={0}
                className="panel w-full cursor-pointer p-5 text-left transition hover:border-slate-300"
              >
                <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                  <div className="min-w-0">
                    <div className="flex items-center gap-3">
                      <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-slate-100 text-slate-600">
                        <FileText size={18} />
                      </div>
                      <div className="min-w-0">
                        <p className="truncate text-sm font-semibold text-slate-900">{project.name}</p>
                        <p className="mt-1 text-xs text-slate-500">创建时间：{formatDate(project.created_at)}</p>
                      </div>
                    </div>
                  </div>

                  <div className="flex flex-wrap items-center gap-3 md:gap-5">
                    <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-semibold ${meta.className}`}>
                      <meta.Icon size={14} className={spinning ? "animate-spin" : ""} />
                      {meta.label}
                    </span>

                    <div className="w-40 min-w-40">
                      <div className="mb-1 flex items-center justify-between text-xs text-slate-500">
                        <span>进度</span>
                        <span>{project.progress}%</span>
                      </div>
                      <div className="h-2 rounded-full bg-slate-100">
                        <div
                          className="h-2 rounded-full bg-[rgb(var(--primary))] transition-all"
                          style={{ width: `${project.progress}%` }}
                        />
                      </div>
                    </div>

                    <button
                      onClick={(e) => handleDeleteProject(e, project.id, project.name)}
                      className="ml-auto inline-flex h-9 w-9 items-center justify-center rounded-lg border border-transparent text-slate-400 transition hover:border-rose-200 hover:bg-rose-50 hover:text-rose-600"
                      aria-label="删除任务"
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                </div>
              </div>
            );
          })}

          {projects.length === 0 && (
            <div className="panel-muted px-6 py-10 text-center text-sm text-slate-500">暂无任务，先上传一个文件开始翻译。</div>
          )}
        </div>
      </section>
    </div>
  );
}