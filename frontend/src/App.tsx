import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Files, Search, GitCompareArrows, LogOut, ArrowUpRight, UploadCloud, FileText, ArrowLeft, RefreshCw, Check, ChevronRight, Plus, Trash2 } from 'lucide-react';
import { api, apiBlob, json, setToken, onUnauthorized, ApiError, display, isPending, type User, type DocumentRecord, type Invoice, type Results, type Reconciliation } from './api';
import './styles.css';
function ErrorNotice({ error }: {
    error: unknown;
}) { if (!error)
    return null; return <div className="notice error" role="alert"><strong>{error instanceof Error ? error.message : 'Something went wrong.'}</strong>{error instanceof ApiError && <small>{error.code}{error.requestId && ` · Request ${error.requestId}`}{error.status === 409 && ' · Reload the document to use its latest version.'}</small>}</div>; }
function Badge({ value }: {
    value: string;
}) { return <span className={`badge ${isPending(value) ? 'pending' : /fail|error|reject/i.test(value) ? 'failed' : /review/i.test(value) ? 'review' : 'done'}`}><i />{value.replaceAll('_', ' ')}</span>; }
function Raw({ value }: {
    value: unknown;
}) {
    if (value == null || (Array.isArray(value) && value.length === 0)) {
        return <p className="record-empty">No entries to show.</p>;
    }
    if (Array.isArray(value)) {
        return <ol className="record-list">{value.map((item, index) => <li key={index}><span className="record-number">{String(index + 1).padStart(2, '0')}</span><Raw value={item}/></li>)}</ol>;
    }
    if (typeof value === 'object') {
        return <dl className="record-fields">{Object.entries(value).map(([key, item]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{typeof item === 'object' && item !== null ? <Raw value={item}/> : display(item)}</dd></div>)}</dl>;
    }
    return <p className="record-text">{display(value)}</p>;
}
function Login({ onLogin }: {
    onLogin: (user: User) => void;
}) { const [email, E] = useState(''), [password, P] = useState(''), [busy, B] = useState(false), [error, S] = useState<unknown>(); return <main className="login"><section className="login-form"><div className="brand"><Files /> Prodoc</div><form onSubmit={async (e) => { e.preventDefault(); B(true); S(undefined); try {
    const data = await api<{
        access_token: string;
        user: User;
    }>('/auth/login', json({ email, password }));
    setToken(data.access_token);
    const me = await api<User | {
        user: User;
    }>('/auth/me');
    onLogin('user' in me ? me.user : me);
}
catch (err) {
    setToken(null);
    S(err);
}
finally {
    B(false);
} }}><h2>Welcome back</h2><p className="muted">Sign in to your document workspace.</p><label>Email address<input type="email" autoComplete="username" required value={email} onChange={e => E(e.target.value)} placeholder="you@company.com"/></label><label>Password<input type="password" autoComplete="current-password" required value={password} onChange={e => P(e.target.value)}/></label><ErrorNotice error={error}/><button className="primary full" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button><p className="fine">Your session ends when you refresh or close this page.</p></form></section></main>; }
type QueueItem = {
    key: string;
    name: string;
    state: string;
    documentId?: string;
    error?: string;
};
export function App() {
    const [user, U] = useState<User | null>(null), [view, V] = useState('documents'), [selected, S] = useState<string | null>(null), [documents, D] = useState<DocumentRecord[]>([]), [error, E] = useState<unknown>(), [loading, L] = useState(false), [queue, Q] = useState<QueueItem[]>([]), [filter, F] = useState('all'), [nameFilter, N] = useState('');
    const fileInput = useRef<HTMLInputElement>(null);
    const session = useRef(0);
    const refresh = useCallback(async () => { const current = session.current; const data = await api<{
        items: DocumentRecord[];
    }>('/documents'); if (current === session.current)
        D(data.items); }, []);
    useEffect(() => { onUnauthorized(() => { session.current++; U(null); D([]); Q([]); S(null); E(new Error('Your session expired. Please sign in again.')); }); return () => onUnauthorized(() => { }); }, []);
    useEffect(() => { if (!user)
        return; let alive = true; L(true); refresh().catch(err => { if (alive)
        E(err); }).finally(() => { if (alive)
        L(false); }); return () => { alive = false; }; }, [user, refresh]);
    useEffect(() => { if (!user)
        return; const ids = [...new Set([...documents.filter(d => isPending(d.status)).map(d => d.id), ...queue.filter(q => q.documentId && isPending(q.state)).map(q => q.documentId!)])]; if (!ids.length)
        return; let active = true; const timer = window.setTimeout(async () => { try {
        await Promise.all(ids.map(async (id) => { const result = await api<{
            status: string;
        }>(`/documents/${encodeURIComponent(id)}/status`); if (active) {
            D(ds => ds.map(d => d.id === id ? { ...d, status: result.status } : d));
            Q(qs => qs.map(q => q.documentId === id ? { ...q, state: result.status } : q));
        } }));
        if (active) {
            await refresh();
            E(undefined);
        }
    }
    catch (err) {
        if (active)
            E(err);
    } }, 2500); return () => { active = false; clearTimeout(timer); }; }, [user, documents, queue, refresh, error]);
    async function upload(files: FileList | null) { if (!files)
        return; const current = session.current; const batch = Array.from(files).map(file => ({ file, key: crypto.randomUUID() })); Q(q => [...q, ...batch.map(({ file, key }) => ({ key, name: file.name, state: 'pending' }))]); for (const { file, key } of batch) {
        if (current !== session.current)
            return;
        Q(q => q.map(i => i.key === key ? { ...i, state: 'uploading' } : i));
        try {
            const body = new FormData();
            body.append('file', file);
            const data = await api<{
                document_id: string;
                job_id: string;
                status_url: string;
            }>('/documents', { method: 'POST', body });
            if (current !== session.current)
                return;
            Q(q => q.map(i => i.key === key ? { ...i, state: 'queued', documentId: data.document_id } : i));
        }
        catch (err) {
            if (current !== session.current)
                return;
            Q(q => q.map(i => i.key === key ? { ...i, state: 'failed', error: err instanceof Error ? err.message : 'Upload failed' } : i));
        }
    } if (current === session.current)
        refresh().catch(E); }
    if (!user)
        return <Login onLogin={u => { session.current++; U(u); E(undefined); V('documents'); }}/>;
    function open(id: string) { S(id); V('documents'); }
    return <div className="shell"><a className="skip-link" href="#workspace-main">Skip to content</a><aside className="sidebar"><a className="brand" href="#" onClick={e => { e.preventDefault(); V('documents'); S(null); }}><Files /> Prodoc</a><nav aria-label="Main navigation">{[{ id: 'documents', label: 'Documents', icon: Files }, { id: 'search', label: 'Search', icon: Search }, { id: 'reconcile', label: 'Reconciliation', icon: GitCompareArrows }].map(({ id, label, icon: Icon }) => <button key={id} aria-current={view === id ? 'page' : undefined} className={view === id ? 'active' : ''} onClick={() => { V(id); S(null); E(undefined); }}><Icon size={18}/>{label}</button>)}</nav><div className="sidebar-bottom"><div className="account"><div className="avatar">{user.email.slice(0, 1).toUpperCase()}</div><div><strong title={user.email}>{user.email}</strong><small>{user.role}</small></div><button className="icon-button" aria-label="Sign out" onClick={() => { session.current++; setToken(null); U(null); D([]); Q([]); S(null); }}><LogOut size={17}/></button></div></div></aside><main className="main" id="workspace-main" tabIndex={-1}><div className="content"><ErrorNotice error={error}/>{view === 'documents' ? (selected ? <Detail id={selected} onBack={() => { S(null); refresh().catch(E); }} onUpdated={() => refresh().catch(E)}/> : <><div className="page-heading"><div><h1>Your documents</h1><p className="muted">Upload invoices, track processing and review the results.</p></div><button className="primary" onClick={() => fileInput.current?.click()}><UploadCloud size={18}/>Upload documents</button></div><input ref={fileInput} type="file" multiple accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.xlsx,.xls,.csv" className="sr-only" aria-label="Upload documents" onChange={e => { void upload(e.target.files); e.target.value = ''; }}/><div className="dropzone" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); void upload(e.dataTransfer.files); }}><div className="upload-icon"><UploadCloud size={22}/></div><div><p>Drop files here, or <button className="text-button" onClick={() => fileInput.current?.click()}>browse</button></p><small>PDF, JPG, PNG, TIFF, XLSX, XLS or CSV. Up to 100 MB each, 50 files at a time.</small></div></div>{queue.length > 0 && <section className="panel queue" aria-label="Upload queue"><div className="section-heading"><h2>Upload queue</h2><button className="text-button" onClick={() => Q(q => q.filter(i => isPending(i.state) || i.state === 'uploading'))}>Clear finished</button></div>{queue.map(q => <div className="queue-row" key={q.key}><FileText size={18}/><span>{q.name}{q.error && <small className="error-text">{q.error}</small>}</span><Badge value={q.state}/>{q.documentId && <button className="text-button" onClick={() => open(q.documentId!)}>Open</button>}</div>)}</section>}<div className="stat-row">{[['Documents', documents.length], ['Needs review', documents.filter(d => /review/i.test(d.status)).length], ['Processing', documents.filter(d => isPending(d.status)).length], ['Completed', documents.filter(d => d.status === 'completed').length], ['Failed', documents.filter(d => /fail|error/i.test(d.status)).length]].map(([label, value]) => <div className="stat" key={label}><strong>{value}</strong><span>{label}</span></div>)}</div><section className="panel"><div className="section-heading"><div className="tabs" aria-label="Document filters">{['all', 'needs_review', 'processing', 'failed'].map(f => <button key={f} aria-pressed={filter === f} className={filter === f ? 'selected' : ''} onClick={() => F(f)}>{f === 'all' ? 'All documents' : f.replaceAll('_', ' ')}</button>)}</div><div className="heading-actions"><input className="filter-input" type="search" aria-label="Filter documents by name" placeholder="Filter by name" value={nameFilter} onChange={e => N(e.target.value)}/><button className="icon-button" aria-label="Refresh documents" onClick={() => { E(undefined); refresh().catch(E); }}><RefreshCw size={17}/></button></div></div>{loading ? <div className="empty" role="status">Loading documents…</div> : <DocumentTable documents={documents.filter(d => d.filename.toLowerCase().includes(nameFilter.trim().toLowerCase()) && (filter === 'all' || (filter === 'processing' ? isPending(d.status) : filter === 'failed' ? /fail|error/i.test(d.status) : /review/i.test(d.status))))} onOpen={open}/>}</section></>) : view === 'search' ? <SearchView onOpen={open}/> : <Reconcile documents={documents}/>}</div></main></div>;
}
function DocumentTable({ documents, onOpen }: {
    documents: DocumentRecord[];
    onOpen: (id: string) => void;
}) { if (!documents.length)
    return <div className="empty"><Files size={32}/><h3>No documents to show</h3><p>Upload a document or try a different filter.</p></div>; return <div className="table-scroll"><table><thead><tr><th>Document</th><th>Type</th><th>Status</th><th>Pages</th><th>Added</th><th><span className="sr-only">Actions</span></th></tr></thead><tbody>{documents.map(d => <tr key={d.id}><td><button className="document-link" onClick={() => onOpen(d.id)}><span className="file-icon"><FileText size={18}/></span><span>{d.filename}{d.error_code && <small>{d.error_code.replaceAll('_', ' ')}</small>}</span></button></td><td><span className="type-tag">{d.filename.split('.').pop()?.toUpperCase()}</span></td><td><Badge value={d.status}/></td><td>{d.page_count ?? '—'}</td><td>{d.created_at ? new Date(d.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) : '—'}</td><td><button className="icon-button" aria-label={`Open ${d.filename}`} onClick={() => onOpen(d.id)}><ArrowUpRight size={18}/></button></td></tr>)}</tbody></table></div>; }
type PageRecord = {
    page?: number;
    sheet?: string;
    text?: string;
};
function SourcePanel({ id, pages, page, onPage }: {
    id: string;
    pages: PageRecord[];
    page: number;
    onPage: (page: number) => void;
}) { const [image, S] = useState<string>(), [state, T] = useState<'loading' | 'image' | 'text'>('loading'), total = Math.max(pages.length, 1), current = pages.find(p => p.page === page) ?? pages[page - 1]; useEffect(() => { let active = true, url: string | undefined; T('loading'); apiBlob(`/documents/${encodeURIComponent(id)}/preview/${page}`).then(blob => { if (!active)
    return; url = URL.createObjectURL(blob); S(url); T('image'); }).catch(() => { if (active)
    T('text'); }); return () => { active = false; if (url)
    URL.revokeObjectURL(url); }; }, [id, page]); return <section className="source-panel" aria-label="Source document"><div className="source-toolbar"><strong>{current?.sheet ? `Sheet: ${current.sheet}` : 'Original document'}</strong><div className="pager"><button className="icon-button" aria-label="Previous page" disabled={page <= 1} onClick={() => onPage(page - 1)}><ChevronRight size={16} className="flip"/></button><span>Page {page} of {total}</span><button className="icon-button" aria-label="Next page" disabled={page >= total} onClick={() => onPage(page + 1)}><ChevronRight size={16}/></button></div></div><div className="source-view">{state === 'loading' ? <div className="empty" role="status">Loading page…</div> : state === 'image' ? <img src={image} alt={`Page ${page} of the original document`}/> : <pre className="source-text">{current?.text || 'No preview is available for this page.'}</pre>}</div></section>; }
function EvidenceTable({ data, onPage }: {
    data: Record<string, unknown>;
    onPage: (page: number) => void;
}) { const evidence = (data.evidence ?? {}) as Record<string, { page?: number; source_text?: string; grounded?: boolean; status?: string; } | undefined>, rows = Object.entries(evidence).filter(([, e]) => e && typeof e === 'object'); if (!rows.length)
    return <div className="empty">No source evidence recorded for this invoice.</div>; return <div className="table-scroll"><table className="evidence-table"><thead><tr><th>Field</th><th>Extracted value</th><th>Source text</th><th>Check</th><th>Page</th></tr></thead><tbody>{rows.map(([field, e]) => <tr key={field}><td>{field.replaceAll('_', ' ')}</td><td>{display(data[field])}</td><td>{e?.source_text ? e.source_text.replace(/\*\*|__/g, '').replace(/\s*\|\s*/g, ' ').trim() : '—'}</td><td><span className={'badge ' + (e?.grounded ? '' : 'failed')}>{e?.grounded ? 'Found in source' : (e?.status ?? 'Not verified').replaceAll('_', ' ')}</span></td><td>{e?.page ? <button className="text-button" onClick={() => onPage(e.page!)}>Page {e.page}</button> : '—'}</td></tr>)}</tbody></table></div>; }
type Finding = {
    rule_id?: string;
    message?: string;
    severity?: string;
    field?: string;
    line?: number;
};
const findingsOf = (data: Record<string, unknown>) => (Array.isArray(data.findings) ? data.findings : []) as Finding[];
// One rule can fire on every line; show it once with the affected lines.
function groupFindings(findings: Finding[]) { const groups = new Map<string, Finding & { lines: number[]; }>(); for (const f of findings) {
    const key = `${f.rule_id}|${f.field ?? ''}|${f.message}`, group = groups.get(key) ?? { ...f, lines: [] };
    if (typeof f.line === 'number')
        group.lines.push(f.line);
    groups.set(key, group);
} return [...groups.values()]; }
function Findings({ findings }: {
    findings: Finding[];
}) { if (!findings.length)
    return <p className="record-empty">No validation issues on this invoice.</p>; return <div className="table-scroll"><table><thead><tr><th>Severity</th><th>Issue</th><th>Where</th></tr></thead><tbody>{groupFindings(findings).map((f, i) => <tr key={i}><td><Badge value={f.severity === 'error' ? 'error' : 'needs_review'}/></td><td>{f.message ?? f.rule_id}</td><td>{f.field ? f.field.replaceAll('_', ' ') : f.lines.length > 1 ? `${f.lines.length} lines` : f.lines.length ? `Line ${f.lines[0]}` : 'Invoice'}</td></tr>)}</tbody></table></div>; }
const STAGES = [['queued', 'Queued'], ['extracting', 'Extract'], ['validating', 'Validate'], ['reconciling', 'Reconcile'], ['needs_review', 'Review'], ['completed', 'Done']];
function Progress({ status }: {
    status: string;
}) { const at = STAGES.findIndex(([id]) => id === status); if (at < 0)
    return null; return <ol className={`stepper${isPending(status) ? '' : ' resting'}`} aria-label="Processing progress">{STAGES.map(([id, label], i) => <li key={id} className={status === 'completed' || i < at ? 'done' : i === at ? 'current' : ''} aria-current={i === at ? 'step' : undefined}>{label}</li>)}</ol>; }
function Audit({ items }: {
    items: {
        action?: string;
        created_at?: string;
        detail?: unknown;
    }[];
}) { if (!items.length)
    return <p className="record-empty">No entries to show.</p>; return <div className="table-scroll"><table><thead><tr><th>When</th><th>Action</th><th>Details</th></tr></thead><tbody>{items.map((a, i) => <tr key={i}><td>{a.created_at ? new Date(a.created_at).toLocaleString() : '—'}</td><td>{a.action?.replaceAll('_', ' ')}</td><td><pre className="audit-detail">{a.detail && Object.keys(a.detail).length ? display(a.detail) : '—'}</pre></td></tr>)}</tbody></table></div>; }
async function downloadMarkdown(id: string, filename: string) { const blob = await apiBlob(`/documents/${encodeURIComponent(id)}/artifacts/document.md`), url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url; link.download = filename.replace(/\.[^.]+$/, '') + '.md'; link.click(); URL.revokeObjectURL(url); }
function Detail({ id, onBack, onUpdated }: {
    id: string;
    onBack: () => void;
    onUpdated: () => void;
}) { const [result, R] = useState<Results>(), [error, E] = useState<unknown>(), [tab, T] = useState('extraction'), [busy, B] = useState(false), [audit, A] = useState<unknown[]>(), [invoiceId, I] = useState(''), [revision, Rev] = useState(0), [sourcePage, P] = useState(1); const load = useCallback(async () => { R(await api<Results>(`/documents/${encodeURIComponent(id)}/results`)); }, [id]); useEffect(() => { let active = true; R(undefined); E(undefined); api<Results>(`/documents/${encodeURIComponent(id)}/results`).then(r => { if (active)
    R(r); }).catch(err => { if (active)
    E(err); }); return () => { active = false; }; }, [id, revision]); useEffect(() => { if (!result || !isPending(result.document.status))
    return; let active = true; const timer = setTimeout(async () => { try {
    const status = await api<{
        status: string;
    }>(`/documents/${encodeURIComponent(id)}/status`);
    if (active) {
        if (isPending(status.status))
            R(r => r ? { ...r, document: { ...r.document, status: status.status } } : r);
        else
            Rev(v => v + 1);
    }
}
catch (err) {
    if (active)
        E(err);
} }, 2500); return () => { active = false; clearTimeout(timer); }; }, [id, result]); const invoice = result?.invoices.find(i => i.id === invoiceId) || result?.invoices[0], startPage = invoice?.data.page_start; useEffect(() => { if (typeof startPage === 'number')
    P(startPage); }, [invoice?.id, startPage]); const pages = (result?.pages ?? []) as PageRecord[], showSource = pages.length > 0 && (tab === 'extraction' || tab === 'evidence'); return <><button className="back text-button" onClick={onBack}><ArrowLeft size={16}/>All documents</button><div className="page-heading"><div><h1>{result?.document.filename || 'Document'}</h1>{result && <><Badge value={result.document.status}/><Progress status={result.document.status}/><p className="fine">Extraction provider: {display(result.document.metadata?.provider ?? 'Not processed')} · Model: {display(result.document.metadata?.model)}</p>{(() => { const m = result.document.metadata as Record<string, any> | undefined; const flags: string[] = m?.review_flags ?? []; const seg = m?.segmentation; const skipped: number[] = seg?.skipped_pages ?? []; const labels: Record<string, string> = { instruction_like_text_detected: 'Contains instruction-like text aimed at AI systems (treated as data, not obeyed). Verify values carefully.', low_financial_signal: 'Weak financial content: confirm this is an invoice or financial record.', not_a_financial_document: 'No financial content detected.' }; return (flags.length > 0 || skipped.length > 0 || (seg && seg.mode === 'segmented')) ? <div className="notice guard" role="status">{flags.map(f => <p key={f}><strong>Review flag:</strong> {labels[f] ?? f.replaceAll('_', ' ')}</p>)}{seg?.mode === 'segmented' && <p>Long document split into {seg.segments} invoice segments, extracted in {seg.requests} requests.</p>}{skipped.length > 0 && <p>{skipped.length} page(s) without financial content were not extracted: {skipped.slice(0, 20).join(', ')}{skipped.length > 20 ? '…' : ''}.</p>}</div> : null; })()}{result.document.error_code && <p className="notice" role="alert">{result.document.error_code === 'not_a_financial_document' ? 'Rejected: this file has no financial content, so it was not sent to the AI model.' : `Processing failed: ${result.document.error_code.replaceAll('_', ' ')}`}</p>}</>}</div><button className="secondary" disabled={busy || !result || isPending(result.document.status)} onClick={async () => { B(true); E(undefined); try {
    await api(`/documents/${encodeURIComponent(id)}/reprocess`, { method: 'POST' });
    Rev(v => v + 1);
    onUpdated();
}
catch (err) {
    E(err);
}
finally {
    B(false);
} }}><RefreshCw size={16}/>{busy ? 'Requesting…' : 'Reprocess'}</button></div><ErrorNotice error={error}/><div className="panel"><div className="section-heading"><div className="tabs">{['extraction', 'observation', 'evidence', 'findings', 'pages', 'audit'].map(t => <button aria-pressed={tab === t} className={tab === t ? 'selected' : ''} key={t} onClick={() => { T(t); if (t === 'audit')
    api<{
        items: unknown[];
    }>(`/documents/${encodeURIComponent(id)}/audit`).then(d => A(d.items)).catch(E); }}>{t}{t === 'findings' && invoice && findingsOf(invoice.data).length > 0 && <span className="count">{groupFindings(findingsOf(invoice.data)).length}</span>}</button>)}</div><div className="heading-actions">{result?.document.metadata?.artifact_run_id ? <button className="text-button" onClick={() => { downloadMarkdown(id, result.document.filename).catch(E); }}>Download Markdown</button> : null}<button className="text-button" onClick={() => { E(undefined); load().catch(E); }}>Reload</button></div></div>{!result ? <div className="empty" role="status">{error ? 'Document results unavailable. Use Reload to retry.' : 'Loading document…'}</div> : <div className="panel-body">{result.invoices.length > 1 && <label className="invoice-picker">Invoice<select value={invoice?.id} onChange={e => I(e.target.value)}>{result.invoices.map(i => <option value={i.id} key={i.id}>{display(i.data.invoice_number)}{typeof i.data.page_start === 'number' ? ` · page${i.data.page_end !== i.data.page_start ? 's' : ''} ${i.data.page_start}${i.data.page_end !== i.data.page_start ? `–${i.data.page_end}` : ''}` : ''}</option>)}</select></label>}<div className={showSource ? 'review-split' : undefined}>{showSource && <SourcePanel id={id} pages={pages} page={Math.min(sourcePage, pages.length)} onPage={P}/>}<div className="review-main">{tab === 'extraction' ? (invoice ? <Review key={`${invoice.id}-${invoice.version}`} invoice={invoice} finalizationBlocked={result.document.metadata?.finalization_blocked === true} onPage={P} onUpdate={next => { R(r => r ? { ...r, invoices: r.invoices.map(i => i.id === next.id ? next : i) } : r); onUpdated(); }}/> : <div className="empty">No extracted invoices yet. Processing may still be underway.</div>) : tab === 'observation' ? <><h2>Layout observation</h2><p className="muted">Model-proposed layout and grouping, not verified facts. Check source coverage before approval.</p><Raw value={result.document.metadata?.observation}/><h3>Coverage</h3><Raw value={result.document.metadata?.coverage}/>{result.document.metadata?.finalization_blocked === true && <p className="error-text" role="alert">Incomplete source coverage blocks finalization. Inspect the source and reprocess.</p>}<h3>Model runs</h3><Raw value={result.document.metadata?.extraction_runs}/></> : tab === 'evidence' ? <><h2>Source evidence</h2><p className="muted">Extracted source references, page locations, and supporting text.</p>{invoice ? <EvidenceTable data={invoice.data as Record<string, unknown>} onPage={P}/> : <div className="empty">No extracted invoices yet.</div>}</> : tab === 'findings' ? <><h2>Validation findings</h2>{invoice && <p className="muted">Review required: {invoice.data.needs_review ? 'Yes' : 'No'}</p>}<h3>Issues</h3><Findings findings={invoice ? findingsOf(invoice.data) : []}/></> : tab === 'pages' ? <><h2>Document pages</h2>{pages.length ? pages.map((p, i) => <article className="page-text" key={i}><h3>{p.sheet ? `Sheet: ${p.sheet}` : `Page ${p.page ?? i + 1}`}</h3><pre className="source-text">{p.text || 'No text recovered from this page.'}</pre></article>) : <p className="record-empty">No pages to show.</p>}</> : <><h2>Audit trail</h2>{audit ? <Audit items={audit as Parameters<typeof Audit>[0]['items']}/> : <p>Loading audit trail…</p>}</>}</div></div></div>}</div></>; }
const reviewFields = ['invoice_number', 'invoice_date', 'supplier_name', 'customer_name', 'currency', 'subtotal', 'tax', 'discount', 'other_charges', 'total', 'line_amount_basis'];
const lineColumns = [['description', 'Description'], ['quantity', 'Quantity'], ['unit_price', 'Unit price'], ['line_total', 'Line total']];
function FieldNote({ data, field, onPage }: {
    data: Record<string, unknown>;
    field: string;
    onPage?: (page: number) => void;
}) { const issue = findingsOf(data).find(f => f.field === field), source = (data.evidence as Record<string, { page?: number; grounded?: boolean; method?: string; } | undefined> | undefined)?.[field]; if (issue)
    return <small className="field-note bad">{issue.message}</small>; if (source?.method === 'human_correction')
    return <small className="field-note">Corrected by reviewer</small>; if (source?.grounded)
    return <small className="field-note ok">{source.page && onPage ? <button type="button" className="text-button" onClick={() => onPage(source.page!)}>Source: page {source.page}</button> : 'Found in document'}</small>; return <small className="field-note">{data[field] == null ? 'Not in document' : 'Not verified'}</small>; }
function LineItems({ value, disabled, onChange }: {
    value: string;
    disabled: boolean;
    onChange: (value: string) => void;
}) { const rows = JSON.parse(value) as Record<string, unknown>[], save = (next: Record<string, unknown>[]) => onChange(JSON.stringify(next, null, 2)); return <><div className="table-scroll"><table className="line-table"><thead><tr>{lineColumns.map(([id, label]) => <th key={id} className={id === 'description' ? undefined : 'amount'}>{label}</th>)}<th className="row-action"><span className="sr-only">Remove</span></th></tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{lineColumns.map(([id, label]) => <td key={id} className={id === 'description' ? undefined : 'amount'}><input aria-label={`Line ${index + 1} ${label}`} disabled={disabled} inputMode={id === 'description' ? undefined : 'decimal'} value={row[id] == null ? '' : String(row[id])} onChange={e => save(rows.map((r, i) => i === index ? { ...r, [id]: e.target.value === '' ? null : e.target.value } : r))}/></td>)}<td className="row-action"><button type="button" className="icon-button" aria-label={`Remove line ${index + 1}`} disabled={disabled} onClick={() => save(rows.filter((_, i) => i !== index))}><Trash2 size={15}/></button></td></tr>)}</tbody></table></div>{rows.length === 0 && <p className="record-empty">No line items extracted.</p>}<button type="button" className="text-button" disabled={disabled} onClick={() => save([...rows, { description: null, quantity: null, unit_price: null, line_total: null }])}><Plus size={15}/>Add line</button></>; }
function Review({ invoice, onUpdate, onPage, finalizationBlocked = false }: {
    invoice: Invoice;
    onUpdate: (invoice: Invoice) => void;
    onPage?: (page: number) => void;
    finalizationBlocked?: boolean;
}) { const [edits, S] = useState<Record<string, string>>({}), [reason, R] = useState(''), [busy, B] = useState(false), [error, E] = useState<unknown>(), [message, M] = useState(''); async function submit(finalize = false) { E(undefined); M(''); B(true); try {
    const changes: Record<string, unknown> = {};
    for (const [field, value] of Object.entries(edits)) {
        if (display(invoice.data[field]) === value)
            continue;
        if (field === 'line_items') {
            let rows: unknown;
            try { rows = JSON.parse(value); } catch { throw new Error('Line items must be valid JSON.'); }
            if (!Array.isArray(rows) || !rows.every(row => row !== null && typeof row === 'object' && !Array.isArray(row)))
                throw new Error('Line items must be a JSON array of objects.');
            changes[field] = rows;
        }
        else if (['subtotal', 'tax', 'discount', 'other_charges', 'total'].includes(field)) {
            if (value.trim() === '' || !Number.isFinite(Number(value)))
                throw new Error(`${field} must be a valid number.`);
            // Money crosses the API as decimal text, never binary floating point.
            changes[field] = value.trim();
        }
        else
            changes[field] = value;
    }
    if (!finalize && !Object.keys(changes).length)
        throw new Error('Change at least one field before saving.');
    if (!finalize && !reason.trim())
        throw new Error('A correction reason is required.');
    const next = await api<Invoice>(`/invoices/${encodeURIComponent(invoice.id)}/${finalize ? 'finalize' : 'corrections'}`, json(finalize ? { version: invoice.version } : { version: invoice.version, changes, reason: reason.trim() }));
    onUpdate(next);
    S({});
    R('');
    M(finalize ? 'Invoice finalized.' : 'Corrections saved.');
}
catch (err) {
    E(err);
}
finally {
    B(false);
} } const dirty = Object.entries(edits).some(([key, value]) => display(invoice.data[key]) !== value); const locked = /finalized|approved/i.test(invoice.review_status), issues = findingsOf(invoice.data), issueCount = groupFindings(issues).length; return <><div className="review-heading"><div><h2>Extracted invoice</h2><p className="muted">Check each field against the original.</p></div><div className="version">Version {invoice.version}<Badge value={invoice.review_status}/></div></div><ErrorNotice error={error}/>{message && <div className="notice success" role="status">{message}</div>}{issues.length > 0 && <div className="notice guard" role="status">{issueCount} validation {issueCount === 1 ? 'issue needs' : 'issues need'} review. See the highlighted fields and the Findings tab.</div>}<form onSubmit={e => { e.preventDefault(); void submit(); }}><div className="field-grid">{reviewFields.map(field => <div key={field} className={`field${issues.some(f => f.field === field) ? ' issue' : ''}`}><label>{field.replaceAll('_', ' ')}<input disabled={busy || locked} value={edits[field] ?? (invoice.data[field] == null ? '' : String(invoice.data[field]))} inputMode={['subtotal', 'tax', 'discount', 'other_charges', 'total'].includes(field) ? 'decimal' : undefined} onChange={e => S(s => ({ ...s, [field]: e.target.value }))}/></label><FieldNote data={invoice.data} field={field} onPage={onPage}/></div>)}</div><h3>Line items</h3><LineItems disabled={busy || locked} value={edits.line_items ?? JSON.stringify(invoice.data.line_items ?? [], null, 2)} onChange={value => S(s => ({ ...s, line_items: value }))}/><p className="fine">Leave a value empty when the document does not show it; missing amounts are not treated as zero.</p>{finalizationBlocked && <p className="error-text" role="alert">Incomplete source coverage blocks finalization. Inspect the observation and reprocess.</p>}<div className="review-actions"><label>Reason for correction<textarea disabled={busy || locked} value={reason} onChange={e => R(e.target.value)} placeholder="Explain what changed and why…" rows={2}/></label><div className="button-row"><button className="secondary" type="submit" disabled={busy || locked || !dirty}>Save corrections</button><button className="primary" type="button" disabled={busy || locked || dirty || finalizationBlocked} onClick={() => void submit(true)}><Check size={17}/>{locked ? 'Finalized' : busy ? 'Saving…' : 'Finalize review'}</button></div><p className="fine">Save corrections before finalizing. Finalized invoices are read-only.</p></div></form></>; }
type SearchMatch = {
    type: 'exact' | 'fuzzy' | 'semantic';
    score: number;
    snippet: string;
    highlights: [number, number][];
};
type SearchItem = DocumentRecord & {
    matches: SearchMatch[];
};
const MATCH_LABEL = { exact: 'Exact match', fuzzy: 'Similar spelling', semantic: 'Related meaning' } as const;
function Highlighted({ text, ranges }: {
    text: string;
    ranges: [number, number][];
}) { const parts: React.ReactNode[] = []; let at = 0; ranges.filter(([a, b]) => a >= at && b > a).forEach(([a, b], i) => { if (a < at)
    return; parts.push(<span key={'t' + i}>{text.slice(at, a)}</span>, <mark key={'m' + i}>{text.slice(a, b)}</mark>); at = b; }); parts.push(<span key="end">{text.slice(at)}</span>); return <>{parts}</>; }
function SearchView({ onOpen }: {
    onOpen: (id: string) => void;
}) { const [query, Q] = useState(''), [mode, M] = useState<'smart' | 'keyword' | 'semantic'>('smart'), [items, I] = useState<SearchItem[]>(), [busy, B] = useState(false), [error, E] = useState<unknown>(), [warning, W] = useState(''), latest = useRef(0);
    const run = async (text: string, selected = mode) => { const term = text.trim(); if (!term) {
        I(undefined);
        return;
    } const ticket = ++latest.current; B(true); E(undefined); try {
        const data = await api<{
            items: SearchItem[];
            warning?: string;
        }>('/search/smart', json({ query: term, modes: selected === 'smart' ? ['keyword', 'semantic'] : [selected] }));
        if (ticket === latest.current) {
            I(data.items);
            W(data.warning || '');
        }
    }
    catch (err) {
        if (ticket === latest.current)
            E(err);
    }
    finally {
        if (ticket === latest.current)
            B(false);
    } };
    // Search as you type: wait for a short pause; ignore one-letter queries.
    useEffect(() => { if (query.trim().length < 2)
        return; const timer = setTimeout(() => { void run(query); }, 400); return () => clearTimeout(timer); }, [query, mode]);
    const modes = [{ id: 'smart', title: 'Smart', hint: 'Exact, similar spelling and meaning together' }, { id: 'keyword', title: 'Keyword', hint: 'Words, invoice numbers and amounts, typo-tolerant' }, { id: 'semantic', title: 'Semantic', hint: 'Related concepts and meaning' }] as const;
    return <><div className="page-heading"><div><h1>Search your collection</h1><p className="muted">Exact words, similar spellings or related meaning. Each result shows why it matched.</p></div></div><section className="panel panel-body"><form onSubmit={e => { e.preventDefault(); void run(query); }}><fieldset className="search-modes"><legend>Search mode</legend>{modes.map(m => <label key={m.id}><input type="radio" name="mode" value={m.id} checked={mode === m.id} onChange={() => M(m.id)}/><span><strong>{m.title}</strong><small>{m.hint}</small></span></label>)}</fieldset><div className="search-input"><Search size={20}/><input aria-label="Search query" required value={query} onChange={e => Q(e.target.value)} placeholder="Try NW-2026-0417, Nortwind, 15362.70 or delivery charges…"/><button className="primary" disabled={busy || !query.trim()}>{busy ? 'Searching…' : 'Search'}</button></div></form><ErrorNotice error={error}/>{warning && <p className="fine">Semantic index unavailable; showing keyword results only.</p>}</section>{items && <section className="panel"><div className="section-heading"><h2>Search results</h2><span className="muted">{items.length} {items.length === 1 ? 'result' : 'results'}</span></div>{items.length === 0 ? <div className="empty"><h3>No matching documents</h3><p>Try fewer words, another spelling, or Semantic mode.</p></div> : <ul className="search-results">{items.map(item => <li key={item.id}><button className="document-link" onClick={() => onOpen(item.id)}><span className="file-icon"><FileText size={17}/></span><span>{item.filename}<small>{item.status.replaceAll('_', ' ')}</small></span></button><div className="match-list">{item.matches.map((m, i) => <div className="match-row" key={i}><span className={'match-type ' + m.type}>{MATCH_LABEL[m.type]}<small>{m.type === 'semantic' ? `similarity ${m.score.toFixed(2)}` : `score ${m.score.toFixed(2)}`}</small></span><p className="snippet"><Highlighted text={m.snippet} ranges={m.highlights}/></p></div>)}</div></li>)}</ul>}</section>}</>; }
function Reconcile({ documents }: {
    documents: DocumentRecord[];
}) { const [options, O] = useState<{
    invoice: Invoice;
    document: DocumentRecord;
}[]>([]), [left, L] = useState(''), [right, R] = useState(''), [loading, Loading] = useState(true), [busy, B] = useState(false), [error, E] = useState<unknown>(), [result, S] = useState<Reconciliation>(), [history, H] = useState<Reconciliation[]>([]); useEffect(() => { let active = true; async function load() { Loading(true); const all = await Promise.allSettled(documents.filter(d => !isPending(d.status) && !/fail|error/i.test(d.status)).map(async (d) => { const r = await api<Results>(`/documents/${encodeURIComponent(d.id)}/results`); return r.invoices.map(invoice => ({ invoice, document: d })); })); if (active) {
    O(all.flatMap(r => r.status === 'fulfilled' ? r.value : []));
    if (all.some(r => r.status === 'rejected'))
        E(new Error('Some invoice options could not be loaded. Return to Documents and retry.'));
    Loading(false);
} } load().catch(err => { if (active) {
    E(err);
    Loading(false);
} }); api<{
    items: Reconciliation[];
}>('/reconciliations').then(d => { if (active)
    H(d.items); }).catch(err => { if (active)
    E(err); }); return () => { active = false; }; }, [documents]); const a = options.find(o => o.invoice.id === left)?.invoice, b = options.find(o => o.invoice.id === right)?.invoice; return <><div className="page-heading"><div><h1>Reconciliation</h1><p className="muted">Compare two invoices and investigate the differences.</p></div></div><ErrorNotice error={error}/><form className="panel panel-body" onSubmit={async (e) => { e.preventDefault(); B(true); E(undefined); try {
    const r = await api<Reconciliation>('/reconciliations/run', json({ left_invoice_id: left, right_invoice_id: right }));
    S(r);
    const h = await api<{
        items: Reconciliation[];
    }>('/reconciliations');
    H(h.items);
}
catch (err) {
    E(err);
}
finally {
    B(false);
} }}><div className="compare-selectors">{[{ label: 'Left invoice', value: left, set: L, other: right }, { label: 'Right invoice', value: right, set: R, other: left }].map(({ label, value, set, other }) => <label key={label}>{label}<select required value={value} onChange={e => { set(e.target.value); S(undefined); }} disabled={loading || busy}><option value="">{loading ? 'Loading invoices…' : 'Select an invoice'}</option>{options.map(o => <option key={o.invoice.id} value={o.invoice.id} disabled={o.invoice.id === other}>{o.document.filename} · {display(o.invoice.data.invoice_number)} · v{o.invoice.version}</option>)}</select></label>)}</div>{!loading && !options.length && <p className="muted">Processed invoices will appear here once documents are ready.</p>}<button className="primary" disabled={busy || !left || !right || left === right}><GitCompareArrows size={17}/>{busy ? 'Comparing…' : 'Run reconciliation'}</button></form>{a && b && <section className="panel"><div className="section-heading"><h2>Invoice comparison</h2><span className="muted">Changed fields are highlighted</span></div><div className="table-scroll"><table><thead><tr><th>Field</th><th>Left invoice</th><th>Right invoice</th><th>Match</th></tr></thead><tbody>{reviewFields.map(f => { const same = JSON.stringify(a.data[f]) === JSON.stringify(b.data[f]); return <tr className={same ? '' : 'diff-row'} key={f}><th>{f.replaceAll('_', ' ')}</th><td>{display(a.data[f])}</td><td>{display(b.data[f])}</td><td>{same ? <span className="match"><Check size={16}/>Same</span> : <span className="difference">Different</span>}</td></tr>; })}</tbody></table></div></section>}{result && <section className="panel panel-body" aria-live="polite"><div className="review-heading"><h2>Reconciliation result</h2><Badge value={result.status}/></div><p className="fine">Run {result.id}</p><Raw value={result}/></section>}<section className="panel"><div className="section-heading"><h2>Previous reconciliations</h2></div>{history.length ? <div className="history">{history.map(h => <button key={h.id} onClick={() => S(h)}><GitCompareArrows size={18}/><span>{h.id}</span><Badge value={h.status}/><ChevronRight size={16}/></button>)}</div> : <div className="empty"><GitCompareArrows size={28}/><h3>A clear comparison starts here</h3><p>Select two invoices to create your first reconciliation.</p></div>}</section></>; }
export { Review, SearchView, Reconcile, Detail };
