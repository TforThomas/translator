import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Check, Edit2, Search, AlertTriangle, Save, X, Loader2, FolderOpen } from "lucide-react";
import { apiGet, apiPostJson } from "../lib/api";
import type { TerminologyItem, ProjectSummary } from "../lib/types";

export default function TermsManagement() {
  const [searchParams] = useSearchParams();
  const projectFromQuery = searchParams.get("project") || "";
  const [terms, setTerms] = useState<TerminologyItem[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string>("");
  const [search, setSearch] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  useEffect(() => {
    const fetchProjects = async () => {
      try {
        const list = await apiGet<ProjectSummary[]>("/api/projects");
        setProjects(list);
        const fromQuery = projectFromQuery ? list.find((p) => p.id === projectFromQuery) : null;
        if (fromQuery) {
          setSelectedProjectId(fromQuery.id);
        } else {
          const pending = list.find((p) => p.status === "pending_terms");
          if (pending) {
            setSelectedProjectId(pending.id);
          } else if (list.length > 0) {
            setSelectedProjectId(list[0].id);
          } else {
            setIsLoading(false);
          }
        }
      } catch (e) {
        console.error(e);
        setIsLoading(false);
      }
    };
    void fetchProjects();
  }, [projectFromQuery]);

  useEffect(() => {
    if (!selectedProjectId) return;
    const fetchTerms = async () => {
      setIsLoading(true);
      try {
        const data = await apiGet<TerminologyItem[]>(`/api/projects/${selectedProjectId}/terms`);
        setTerms(data);
      } catch (e) {
        console.error(e);
      } finally {
        setIsLoading(false);
      }
    };
    void fetchTerms();
  }, [selectedProjectId]);

  const handleConfirm = async (id: string) => {
    try {
      await apiPostJson(`/api/terms/${id}/confirm`, {});
      setTerms(terms.map((t) => (t.id === id ? { ...t, is_confirmed: true } : t)));
    } catch (e) {
      console.error("Failed to confirm term:", e);
    }
  };

  const handleConfirmAll = async () => {
    if (!selectedProjectId) return;
    setIsSaving(true);
    try {
      await apiPostJson(`/api/projects/${selectedProjectId}/terms/confirm_all`, {});
      setTerms(terms.map((t) => ({ ...t, is_confirmed: true })));
    } catch (e) {
      console.error(e);
    } finally {
      setIsSaving(false);
    }
  };

  const startEdit = (id: string) => {
    const term = terms.find((t) => t.id === id);
    if (!term) return;
    setEditingId(id);
    setDraft(term.translated_term);
  };

  const cancelEdit = () => {
    setEditingId(null);
    setDraft("");
  };

  const saveEdit = async () => {
    if (!editingId || !draft) return;
    setIsSaving(true);
    try {
      await apiPostJson(`/api/terms/${editingId}/update`, { translated_term: draft });
      setTerms(terms.map((t) => (t.id === editingId ? { ...t, translated_term: draft } : t)));
      cancelEdit();
    } catch (e) {
      console.error("Failed to save term:", e);
    } finally {
      setIsSaving(false);
    }
  };

  const filteredTerms = useMemo(
    () =>
      terms.filter(
        (t) => t.original_term.toLowerCase().includes(search.toLowerCase()) || t.translated_term.toLowerCase().includes(search.toLowerCase())
      ),
    [terms, search]
  );

  const confirmedCount = terms.filter((t) => t.is_confirmed).length;
  const pendingCount = terms.length - confirmedCount;

  return (
    <div className="space-y-6">
      <section className="page-header">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-slate-900">术语管理</h1>
            <p className="mt-2 text-sm text-slate-600">确认核心名词译法后，后续翻译会保持全书一致。</p>
          </div>

          <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
            <div className="panel-muted px-4 py-3">
              <p className="text-xs text-slate-500">术语总数</p>
              <p className="mt-1 text-lg font-semibold text-slate-900">{terms.length}</p>
            </div>
            <div className="panel-muted px-4 py-3">
              <p className="text-xs text-slate-500">已确认</p>
              <p className="mt-1 text-lg font-semibold text-emerald-700">{confirmedCount}</p>
            </div>
            <div className="panel-muted px-4 py-3">
              <p className="text-xs text-slate-500">待确认</p>
              <p className="mt-1 text-lg font-semibold text-amber-700">{pendingCount}</p>
            </div>
          </div>
        </div>
      </section>

      <section className="panel p-5">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div className="flex flex-1 flex-col gap-3 md:flex-row">
            <label className="w-full md:w-80">
              <span className="field-label">选择项目</span>
              <div className="relative">
                <FolderOpen size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                <select
                  value={selectedProjectId}
                  onChange={(e) => setSelectedProjectId(e.target.value)}
                  className="input-base py-0 pl-9 pr-3 text-slate-700"
                >
                  {projects.length === 0 && <option value="">暂无项目</option>}
                  {projects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} ({p.status === "pending_terms" ? "待确认" : p.status})
                    </option>
                  ))}
                </select>
              </div>
            </label>

            <label className="w-full md:w-72">
              <span className="field-label">搜索术语</span>
              <div className="relative">
                <Search size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                <input
                  type="text"
                  placeholder="按原文或译文检索"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  className="input-base pl-9 pr-3"
                />
              </div>
            </label>
          </div>

          <button
            onClick={() => void handleConfirmAll()}
            disabled={!selectedProjectId || terms.length === 0 || isSaving}
            className="btn-primary xl:ml-3"
          >
            {isSaving ? <Loader2 size={16} className="animate-spin" /> : <Check size={16} />}
            全部确认
          </button>
        </div>
      </section>

      <section className="panel-muted flex items-start gap-3 px-4 py-3">
        <AlertTriangle size={16} className="mt-0.5 text-amber-700" />
        <p className="text-xs leading-5 text-slate-600">
          建议优先确认人名、地名、机构名与专业术语。已确认项会被注入后续翻译上下文，减少译名漂移。
        </p>
      </section>

      <section className="panel overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[860px] border-collapse text-left text-sm leading-6">
            <thead>
              <tr className="border-b border-[rgb(var(--line))] bg-slate-50/80 text-slate-600">
                <th className="px-5 py-3 font-semibold">原文</th>
                <th className="px-5 py-3 font-semibold">译文候选</th>
                <th className="px-5 py-3 font-semibold">类型</th>
                <th className="px-5 py-3 text-center font-semibold">状态</th>
                <th className="px-5 py-3 text-right font-semibold">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[rgb(var(--line))]">
              {filteredTerms.map((term) => (
                <tr key={term.id} className="hover:bg-slate-50/70">
                  <td className="px-5 py-3.5 font-medium text-slate-900">{term.original_term}</td>
                  <td className="px-5 py-3.5">
                    {editingId === term.id ? (
                      <div className="flex items-center gap-2">
                        <input
                          value={draft}
                          onChange={(e) => setDraft(e.target.value)}
                          className="w-full max-w-[280px] rounded-lg border border-[rgb(var(--line))] px-3 py-1.5 text-sm outline-none focus:border-[rgb(var(--primary))]"
                        />
                        <button
                          onClick={saveEdit}
                          className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-600 text-white transition hover:bg-emerald-700"
                          aria-label="保存"
                        >
                          <Save size={14} />
                        </button>
                        <button
                          onClick={cancelEdit}
                          className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-[rgb(var(--line))] text-slate-600 transition hover:bg-slate-50"
                          aria-label="取消"
                        >
                          <X size={14} />
                        </button>
                      </div>
                    ) : (
                      <div className="flex items-center gap-2">
                        <span className={term.is_confirmed ? "font-semibold text-emerald-700" : "font-semibold text-slate-900"}>{term.translated_term}</span>
                        <button className="text-slate-400 transition hover:text-[rgb(var(--primary))]" onClick={() => startEdit(term.id)} aria-label="编辑">
                          <Edit2 size={14} />
                        </button>
                      </div>
                    )}
                  </td>
                  <td className="px-5 py-3.5">
                    <span className="inline-flex rounded-md bg-slate-100 px-2.5 py-1 text-xs text-slate-600">{term.type}</span>
                  </td>
                  <td className="px-5 py-3.5 text-center">
                    {term.is_confirmed ? (
                      <span className="inline-flex items-center gap-1 text-xs font-semibold text-emerald-700">
                        <Check size={14} /> 已确认
                      </span>
                    ) : (
                      <span className="text-xs font-semibold text-amber-700">待确认</span>
                    )}
                  </td>
                  <td className="px-5 py-3.5 text-right">
                    {!term.is_confirmed ? (
                      <button
                        onClick={() => handleConfirm(term.id)}
                        className="rounded-lg border border-[rgb(var(--primary))] px-3 py-1.5 text-xs font-semibold text-[rgb(var(--primary))] transition hover:bg-[rgb(var(--primary-soft))]"
                      >
                        确认
                      </button>
                    ) : (
                      <button onClick={() => startEdit(term.id)} className="rounded-lg px-3 py-1.5 text-xs font-semibold text-slate-500 transition hover:bg-slate-100">
                        修改
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {!isLoading && filteredTerms.length === 0 && <div className="px-6 py-10 text-center text-sm text-slate-500">未找到匹配术语</div>}
        {isLoading && <div className="px-6 py-10 text-center text-sm text-slate-500">加载术语中...</div>}
      </section>
    </div>
  );
}