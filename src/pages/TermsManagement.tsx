import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Check, Edit2, Search, AlertTriangle, Save, X, Loader2, FolderOpen } from "lucide-react";
import { apiGet, apiPostJson } from "../lib/api";
import type { TerminologyItem, ProjectSummary } from "../lib/types";

export default function TermsManagement() {
	const [sp] = useSearchParams();
	const pq = sp.get("project") || "";
	const [terms, setTerms] = useState<TerminologyItem[]>([]);
	const [projects, setProjects] = useState<ProjectSummary[]>([]);
	const [pid, setPid] = useState<string>("");
	const [q, setQ] = useState("");
	const [editing, setEditing] = useState<string | null>(null);
	const [draft, setDraft] = useState("");
	const [loading, setLoading] = useState(true);
	const [saving, setSaving] = useState(false);

	useEffect(() => {
		(async () => {
			try {
				const list = await apiGet<ProjectSummary[]>("/api/projects");
				setProjects(list);
				const fromQ = pq ? list.find((p) => p.id === pq) : null;
				if (fromQ) setPid(fromQ.id);
				else {
					const pending = list.find((p) => p.status === "pending_terms");
					if (pending) setPid(pending.id);
					else if (list.length > 0) setPid(list[0].id);
					else setLoading(false);
				}
			} catch { setLoading(false); }
		})();
	}, [pq]);

	useEffect(() => {
		if (!pid) return;
		(async () => {
			setLoading(true);
			try { setTerms(await apiGet<TerminologyItem[]>(`/api/projects/${pid}/terms`)); }
			catch (e) { console.error(e); }
			finally { setLoading(false); }
		})();
	}, [pid]);

	const confirm = async (id: string) => {
		try { await apiPostJson(`/api/terms/${id}/confirm`, {}); setTerms((ts) => ts.map((t) => t.id === id ? { ...t, is_confirmed: true } : t)); }
		catch (e) { console.error(e); }
	};
	const confirmAll = async () => {
		if (!pid) return;
		setSaving(true);
		try { await apiPostJson(`/api/projects/${pid}/terms/confirm_all`, {}); setTerms((ts) => ts.map((t) => ({ ...t, is_confirmed: true }))); }
		catch (e) { console.error(e); }
		finally { setSaving(false); }
	};
	const startEdit = (id: string) => { const t = terms.find((x) => x.id === id); if (!t) return; setEditing(id); setDraft(t.translated_term); };
	const cancelEdit = () => { setEditing(null); setDraft(""); };
	const saveEdit = async () => {
		if (!editing || !draft) return;
		setSaving(true);
		try { await apiPostJson(`/api/terms/${editing}/update`, { translated_term: draft }); setTerms((ts) => ts.map((t) => t.id === editing ? { ...t, translated_term: draft } : t)); cancelEdit(); }
		catch (e) { console.error(e); }
		finally { setSaving(false); }
	};

	const filtered = useMemo(() => terms.filter((t) => t.original_term.toLowerCase().includes(q.toLowerCase()) || t.translated_term.toLowerCase().includes(q.toLowerCase())), [terms, q]);
	const confirmedCount = terms.filter((t) => t.is_confirmed).length;
	const pendingCount = terms.length - confirmedCount;
	const stats: Array<[string, number, string]> = [
		["术语总数", terms.length, "lp-chip-ink"],
		["已确认",   confirmedCount, "lp-chip-green"],
		["待确认",   pendingCount, "lp-chip-amber"],
	];

	return (
		<div className="lp-stack lp-gap-24">
			<header className="lp-row lp-wrap lp-between lp-gap-16 lp-items-end">
				<div>
					<div className="lp-script-sm">Glossary</div>
					<h1 className="lp-page-title">术语 · <em>一字不苟</em></h1>
					<p className="lp-page-sub">确认核心名词译法后，后续翻译会保持全书一致。</p>
				</div>
				<div className="lp-row lp-wrap lp-gap-10">
					{stats.map(([label, v, cls]) => (
						<div key={label} className="lp-card lp-stat-card">
							<div className="lp-label lp-mb-2">{label}</div>
							<div className="lp-row lp-gap-8">
								<span className="lp-numeric">{v}</span>
								<span className={"lp-chip lp-chip-xs " + cls}>项</span>
							</div>
						</div>
					))}
				</div>
			</header>

			<section className="lp-paper lp-row lp-wrap lp-items-end lp-gap-14">
				<div className="lp-flex1-220">
					<label className="lp-label">选择项目</label>
					<div className="lp-field">
						<FolderOpen size={16} className="lp-field-icon is-accent" />
						<select className="lp-input lp-select lp-input-left" value={pid} onChange={(e) => setPid(e.target.value)}>
							{projects.length === 0 && <option value="">暂无项目</option>}
							{projects.map((p) => (
								<option key={p.id} value={p.id}>{p.name}（{p.status === "pending_terms" ? "待确认" : p.status}）</option>
							))}
						</select>
					</div>
				</div>
				<div className="lp-flex1-220">
					<label className="lp-label">搜索术语</label>
					<div className="lp-field">
						<Search size={16} className="lp-field-icon is-muted" />
						<input className="lp-input lp-input-left" placeholder="原文 / 译文..." value={q} onChange={(e) => setQ(e.target.value)} />
					</div>
				</div>
				<button className="lp-btn lp-btn-primary" onClick={() => void confirmAll()} disabled={!pid || terms.length === 0 || saving}>
					{saving ? <Loader2 size={15} className="animate-spin" /> : <Check size={15} />}
					全部确认
				</button>
			</section>

			<div className="lp-card lp-hint-card">
				<AlertTriangle size={18} color="rgb(196,154,67)" className="lp-hint-icon" />
				<p className="lp-muted lp-m-0 lp-fs-13">
					建议优先确认人名、地名、机构名与专业术语。已确认项会被注入后续翻译上下文，减少译名漂移。
				</p>
			</div>

			<section className="lp-paper lp-paper-flush">
				<table className="lp-table">
					<thead>
						<tr>
							<th>原文</th>
							<th>译文</th>
							<th className="lp-th-100">类型</th>
							<th className="lp-th-120">状态</th>
							<th className="lp-th-120-right">操作</th>
						</tr>
					</thead>
					<tbody>
						{filtered.map((t) => (
							<tr key={t.id}>
								<td className="lp-original">{t.original_term}</td>
								<td>
									{editing === t.id ? (
										<div className="lp-row lp-gap-6">
											<input className="lp-input lp-input-sm" value={draft} onChange={(e) => setDraft(e.target.value)} autoFocus />
											<button className="lp-btn lp-btn-primary lp-btn-sm" onClick={() => void saveEdit()} disabled={saving}><Save size={14} /></button>
											<button className="lp-btn lp-btn-ghost lp-btn-sm" onClick={cancelEdit}><X size={14} /></button>
										</div>
									) : (
										<div className="lp-row lp-gap-8">
											<span className="lp-note-translated">{t.translated_term}</span>
											<button className="lp-btn lp-btn-ghost lp-btn-icon" onClick={() => startEdit(t.id)} aria-label="编辑"><Edit2 size={13} /></button>
										</div>
									)}
								</td>
								<td><span className="lp-chip lp-chip-ink">{t.type}</span></td>
								<td>{t.is_confirmed ? <span className="lp-chip lp-chip-green"><Check size={12} /> 已确认</span> : <span className="lp-chip lp-chip-amber">待确认</span>}</td>
								<td className="lp-td-right">
									{!t.is_confirmed ? (
										<button className="lp-btn lp-btn-primary lp-btn-sm" onClick={() => void confirm(t.id)}>确认</button>
									) : (
										<button className="lp-btn lp-btn-ghost lp-btn-sm" onClick={() => startEdit(t.id)}>修改</button>
									)}
								</td>
							</tr>
						))}
					</tbody>
				</table>
				{!loading && filtered.length === 0 && <div className="lp-muted lp-loader-inline">未找到匹配术语</div>}
				{loading && <div className="lp-loader-inline"><Loader2 className="animate-spin" /></div>}
			</section>
		</div>
	);
}