import React, { useCallback, useEffect, useMemo, useState } from 'react';
import api from '../api';

const scoreOf = (candidate) => Number(candidate.score) || 0;

const Results = () => {
    const [results, setResults] = useState([]);
    const [integrationJobs, setIntegrationJobs] = useState([]);
    const [pipelineBatches, setPipelineBatches] = useState([]);
    const [pipelineData, setPipelineData] = useState(null);
    const [integrationData, setIntegrationData] = useState(null);
    const [integrationPage, setIntegrationPage] = useState(0);
    const [integrationLoading, setIntegrationLoading] = useState(false);
    const [integrationUnavailable, setIntegrationUnavailable] = useState(false);
    const [totalResults, setTotalResults] = useState(0);
    const [loadingMore, setLoadingMore] = useState(false);
    const [selectedId, setSelectedId] = useState(null);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);
    const [query, setQuery] = useState('');
    const [sortBy, setSortBy] = useState('score-desc');
    const [deletingId, setDeletingId] = useState(null);
    const [preview, setPreview] = useState(null);
    const [previewLoading, setPreviewLoading] = useState(false);
    const [exportingXlsx, setExportingXlsx] = useState(false);
    const [retryingBatch, setRetryingBatch] = useState(false);

    useEffect(() => () => {
        if (preview?.objectUrl) URL.revokeObjectURL(preview.objectUrl);
    }, [preview]);

    const loadResults = useCallback(async () => {
        setLoading(true);
        try {
            const [batchResponse, integrationsResponse, pipelineResponse] = await Promise.allSettled([
                api.get('/results', { params: { limit: 50, offset: 0 } }),
                api.get('/integrations/jobs'),
                api.get('/pipeline/batches', { params: { limit: 100 } }),
            ]);
            if (batchResponse.status !== 'fulfilled') throw batchResponse.reason;
            const data = batchResponse.value.data;
            const entries = data.results || [];
            const roles = integrationsResponse.status === 'fulfilled' ? (integrationsResponse.value.data.jobs || []) : [];
            const pipeline = pipelineResponse.status === 'fulfilled' ? (pipelineResponse.value.data.batches || []) : [];
            setResults(entries);
            setIntegrationJobs(roles);
            setPipelineBatches(pipeline);
            setIntegrationUnavailable(integrationsResponse.status === 'rejected' && integrationsResponse.reason.response?.status === 503);
            setTotalResults(data.total ?? entries.length);
            const history = [
                ...entries.map((entry) => ({ ...entry, source: 'batch', historyId: `batch:${entry.id}` })),
                ...roles.map((job) => ({ ...job, source: 'integration', historyId: `integration:${job.id}`, timestamp: job.created_at })),
                ...pipeline.map((job) => ({ ...job, source: 'pipeline', historyId: `pipeline:${job.id}`, timestamp: job.created_at })),
            ].sort((left, right) => new Date(right.timestamp).getTime() - new Date(left.timestamp).getTime());
            setSelectedId((current) => history.some((entry) => entry.historyId === current) ? current : (history[0]?.historyId ?? null));
            setError('');
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not load saved results.');
        } finally {
            setLoading(false);
        }
    }, []);

    const loadMoreResults = async () => {
        setLoadingMore(true);
        try {
            const { data } = await api.get('/results', { params: { limit: 50, offset: results.length } });
            const entries = data.results || [];
            setResults((current) => [...current, ...entries]);
            setTotalResults(data.total ?? totalResults);
            setError('');
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not load older screenings.');
        } finally {
            setLoadingMore(false);
        }
    };

    const history = useMemo(() => [
        ...results.map((entry) => ({ ...entry, source: 'batch', historyId: `batch:${entry.id}` })),
        ...integrationJobs.map((job) => ({ ...job, source: 'integration', historyId: `integration:${job.id}`, timestamp: job.created_at })),
        ...pipelineBatches.map((job) => ({ ...job, source: 'pipeline', historyId: `pipeline:${job.id}`, timestamp: job.created_at })),
    ].sort((left, right) => new Date(right.timestamp).getTime() - new Date(left.timestamp).getTime()), [results, integrationJobs, pipelineBatches]);

    useEffect(() => { loadResults(); }, [loadResults]);

    const selected = history.find((result) => result.historyId === selectedId);
    const candidates = useMemo(() => {
        if (!selected) return [];
        if (selected.source === 'integration') return (integrationData?.job?.id === selected.id ? integrationData.applications || [] : []).map((candidate) => ({
            ...candidate,
            name: candidate.candidate_name,
        }));
        if (selected.source === 'pipeline') return pipelineData?.batch?.id === selected.id ? pipelineData.candidates || [] : [];
        return selected.candidates || [];
    }, [selected, integrationData, pipelineData]);

    useEffect(() => {
        if (!selected || !['integration', 'pipeline'].includes(selected.source)) {
            setIntegrationData(null); setPipelineData(null); setIntegrationLoading(false);
            return undefined;
        }
        let cancelled = false;
        let timer;
        const poll = async () => {
            try {
                const { data } = selected.source === 'integration'
                    ? await api.get(`/integrations/jobs/${selected.id}/applications`, { params: { limit: 50, offset: integrationPage * 50 } })
                    : await api.get(`/pipeline/batches/${selected.id}/candidates`, { params: { limit: 50, offset: integrationPage * 50 } });
                if (cancelled) return;
                if (selected.source === 'integration') setIntegrationData(data);
                else setPipelineData(data);
                setError('');
                const pending = selected.source === 'integration'
                    ? (data.summary?.queued || 0) + (data.summary?.processing || 0) > 0
                    : ['uploaded', 'uploading', 'queued', 'preprocessing', 'processing'].includes(data.batch?.state);
                if ((selected.source === 'integration' && data.job?.status === 'open') || pending) timer = window.setTimeout(poll, 1500);
            } catch (requestError) {
                if (cancelled) return;
                setError(requestError.response?.data?.error || 'Could not load this screening’s candidates.');
                if (selected.status === 'open' || ['queued', 'preprocessing', 'processing'].includes(selected.state)) timer = window.setTimeout(poll, 5000);
            } finally {
                if (!cancelled) setIntegrationLoading(false);
            }
        };
        setIntegrationData(null); setPipelineData(null);
        setIntegrationLoading(true);
        poll();
        return () => { cancelled = true; window.clearTimeout(timer); };
    }, [selected, selected?.historyId, selected?.status, selected?.state, integrationPage]);
    const visibleCandidates = useMemo(() => {
        const filtered = candidates.filter((candidate) => (candidate.name || candidate.filename || '').toLowerCase().includes(query.trim().toLowerCase()));
        return [...filtered].sort((left, right) => {
            if (sortBy === 'score-asc') return scoreOf(left) - scoreOf(right);
            if (sortBy === 'name-asc') return (left.name || '').localeCompare(right.name || '');
            if (sortBy === 'name-desc') return (right.name || '').localeCompare(left.name || '');
            return scoreOf(right) - scoreOf(left);
        });
    }, [candidates, query, sortBy]);

    const scoredCandidates = candidates.filter((candidate) => !candidate.status || candidate.status === 'completed');
    const averageScore = scoredCandidates.length
        ? scoredCandidates.reduce((sum, candidate) => sum + scoreOf(candidate), 0) / scoredCandidates.length
        : 0;
    const topScore = scoredCandidates.length
        ? scoredCandidates.reduce((top, candidate) => Math.max(top, scoreOf(candidate)), Number.NEGATIVE_INFINITY)
        : 0;
    const selectedCandidateTotal = selected?.source === 'integration'
        ? (integrationData?.summary?.total ?? selected.applicant_count ?? 0)
        : selected?.source === 'pipeline'
            ? (pipelineData?.total ?? selected.total_count ?? 0)
            : candidates.length;

    const removeResult = async (id) => {
        setDeletingId(id);
        try {
            await api.delete(`/results/${id}`);
            const remaining = results.filter((result) => result.id !== id);
            setResults(remaining);
            setTotalResults((total) => Math.max(0, total - 1));
            if (selectedId === `batch:${id}`) setSelectedId(remaining[0] ? `batch:${remaining[0].id}` : (integrationJobs[0] ? `integration:${integrationJobs[0].id}` : null));
            setError('');
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not delete this result.');
        } finally {
            setDeletingId(null);
        }
    };

    const retryPipelineBatch = async () => {
        if (!selected || selected.source !== 'pipeline') return;
        setRetryingBatch(true);
        setError('');
        try {
            await api.post(`/pipeline/batches/${selected.id}/start`);
            setPipelineBatches((current) => current.map((batch) => batch.id === selected.id ? { ...batch, state: 'queued', error: null } : batch));
            setPipelineData((current) => current?.batch?.id === selected.id ? { ...current, batch: { ...current.batch, state: 'queued', error: null } } : current);
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not retry this batch.');
        } finally {
            setRetryingBatch(false);
        }
    };

    const exportCsv = async () => {
        const safeText = (value) => {
            const text = String(value ?? '');
            const formulaSafe = /^[=+@-]/.test(text) ? `'${text}` : text;
            return `"${formulaSafe.replace(/"/g, '""')}"`;
        };
        let exportCandidates = visibleCandidates;
        if (selected?.source === 'pipeline') {
            exportCandidates = [];
            for (let offset = 0; offset < (pipelineData?.total || 0); offset += 100) {
                const { data } = await api.get(`/pipeline/batches/${selected.id}/candidates`, { params: { limit: 100, offset } });
                exportCandidates.push(...(data.candidates || []));
            }
            exportCandidates = exportCandidates.filter((candidate) => (candidate.name || candidate.filename || '').toLowerCase().includes(query.trim().toLowerCase()));
        }
        const rows = [
            ['Rank', 'Candidate', 'Match score (0-1)', 'Semantic similarity (0-1)', 'Exact keyword coverage'],
            ...exportCandidates.map((candidate, index) => [
                index + 1,
                safeText(candidate.name || candidate.filename),
                scoreOf(candidate).toFixed(6),
                Number(candidate.semantic_score ?? candidate.score ?? 0).toFixed(6),
                `${((candidate.analysis?.keyword_coverage || 0) * 100).toFixed(1)}%`,
            ]),
        ];
        const csv = rows.map((row) => row.join(',')).join('\r\n');
        const blobUrl = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
        const link = document.createElement('a');
        link.href = blobUrl;
        link.download = `resumemetrics-results-${selected?.id || 'export'}.csv`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    };

    const exportXlsx = async () => {
        if (!selectedId) return;
        setExportingXlsx(true);
        setError('');
        try {
            const { data } = await api.get(`/results/${selected.id}/export.xlsx`, { responseType: 'blob' });
            const blobUrl = URL.createObjectURL(data);
            const link = document.createElement('a');
            link.href = blobUrl;
            link.download = `resumemetrics-results-${selected.id}.xlsx`;
            link.click();
            setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not export this screening as Excel.');
        } finally {
            setExportingXlsx(false);
        }
    };

    const previewCandidate = async (candidate) => {
        setPreviewLoading(true);
        setError('');
        try {
            const objectName = candidate.object_name || candidate.name;
            const encodedName = encodeURIComponent(objectName);
            if ((candidate.filename || candidate.name)?.toLowerCase().endsWith('.docx')) {
                const { data } = await api.get(`/resume-preview/${encodedName}?format=text`);
                setPreview({ candidate, text: data.text || '' });
            } else {
                const { data } = await api.get(`/resume-preview/${encodedName}`, { responseType: 'blob' });
                setPreview({ candidate, objectUrl: URL.createObjectURL(data) });
            }
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not open this resume.');
        } finally {
            setPreviewLoading(false);
        }
    };

    const downloadCandidate = async (candidate) => {
        try {
            const objectName = candidate.object_name || candidate.name;
            const filename = encodeURIComponent(candidate.filename || candidate.name || 'resume');
            const { data } = await api.get(`/resume-preview/${encodeURIComponent(objectName)}?download=1&filename=${filename}`, { responseType: 'blob' });
            const objectUrl = URL.createObjectURL(data);
            const link = document.createElement('a');
            link.href = objectUrl;
            link.download = candidate.filename || candidate.name || 'resume';
            link.click();
            setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not download this resume.');
        }
    };

    return (
        <div className="results-page">
            <header className="results-heading">
                <div>
                    <p className="workspace-eyebrow">SCREENING HISTORY</p>
                    <h1>Ranked candidates</h1>
                    <p>Review candidates from ZIP screenings and connected careers portals in one place.</p>
                </div>
                <button type="button" className="button button--secondary" onClick={loadResults} disabled={loading}>
                    <span aria-hidden="true">↻</span> {loading ? 'Refreshing…' : 'Refresh results'}
                </button>
            </header>

            {error && <div className="workspace-alert results-alert" role="alert">{error}</div>}
            {integrationUnavailable && <div className="integration-notice" role="status">Careers portal results are not enabled on this Cloud Run deployment yet. Batch screening results are still available.</div>}

            <div className="results-layout">
                <aside className="history-card" aria-label="Screenings and connected job roles">
                    <div className="history-card__header"><div><h2>Screenings and roles</h2><p>{results.length} legacy · {pipelineBatches.length} resumable batches · {integrationJobs.length} portal roles</p></div><span className="history-count">{totalResults + pipelineBatches.length + integrationJobs.length}</span></div>
                    {loading && history.length === 0 && <p className="empty-history" role="status">Loading results…</p>}
                    {!loading && history.length === 0 && <div className="empty-history"><span aria-hidden="true">▤</span><strong>No results yet</strong><p>Start with a ZIP batch or connect a careers portal from Workspace.</p></div>}
                    <ul className="history-list">
                        {history.map((result) => (
                            <li key={result.historyId} className={`history-item${selectedId === result.historyId ? ' is-selected' : ''}`}>
                                <button type="button" className="history-item__select" onClick={() => { setSelectedId(result.historyId); setIntegrationPage(0); setQuery(''); }} aria-current={selectedId === result.historyId ? 'true' : undefined}>
                                    <span className="history-item__date">{result.source === 'integration' ? 'CAREERS PORTAL' : 'ZIP BATCH'} · {new Date(result.timestamp).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}</span>
                                    <strong>{result.source === 'integration' ? result.title : result.source === 'pipeline' ? `${result.completed_count || 0} / ${result.total_count || '…'} ranked` : `${(result.candidates || []).length} candidates`}</strong>
                                    <span>{result.source === 'integration' ? `${result.applicant_count || 0} applicants · ${result.status || 'open'}` : result.source === 'pipeline' ? result.state : new Date(result.timestamp).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}</span>
                                </button>
                                {result.source === 'batch' && <button type="button" className="history-item__delete" aria-label={`Delete screening ${result.id}`} title="Delete screening" disabled={deletingId === result.id} onClick={() => removeResult(result.id)}>{deletingId === result.id ? '…' : '×'}</button>}
                            </li>
                        ))}
                    </ul>
                    {results.length < totalResults && <button type="button" className="button button--secondary history-load-more" onClick={loadMoreResults} disabled={loadingMore}>{loadingMore ? 'Loading…' : 'Load older screenings'}</button>}
                </aside>

                <main className="results-main">
                    {!selected ? (
                        <section className="results-empty"><span aria-hidden="true">✳</span><h2>Your shortlist starts here</h2><p>Run a ZIP screening or connect a careers portal from Workspace. Both result types appear here.</p></section>
                    ) : (
                        <>
                            <div className="results-summary">
                                <article className="summary-card"><span>{selected.source === 'integration' ? 'Applicants received' : 'Ranked candidates'}</span><strong>{selectedCandidateTotal}</strong><small>{selected.source === 'integration' ? `${integrationData?.summary?.completed || 0} ranked · ${(integrationData?.summary?.queued || 0) + (integrationData?.summary?.processing || 0)} processing` : selected.source === 'pipeline' ? `${pipelineData?.batch?.completed_count || 0} ranked · ${pipelineData?.batch?.failed_count || 0} failed` : 'In this ZIP screening'}</small></article>
                                <article className="summary-card"><span>Top match score</span><strong>{topScore.toFixed(3)}</strong><small>Weighted score from 0 to 1</small></article>
                                <article className="summary-card"><span>Average match score</span><strong>{averageScore.toFixed(3)}</strong><small>Across completed candidates</small></article>
                            </div>

                            <section className="candidate-card" aria-labelledby="candidate-list-title">
                                <div className="candidate-card__header">
                                    <div><p className="workspace-eyebrow">{selected.source === 'integration' ? `CAREERS PORTAL · ${selected.status || 'open'}` : 'ZIP BATCH REVIEW'}</p><h2 id="candidate-list-title">{selected.source === 'integration' ? selected.title : 'Candidate list'}</h2><p>{selected.source === 'integration' ? 'Applicant scores update as each resume finishes processing.' : selected.source === 'pipeline' ? `${pipelineData?.batch?.state || selected.state} · ${new Date(selected.timestamp).toLocaleString()}` : `Screened ${new Date(selected.timestamp).toLocaleString()}`}</p></div>
                                    <button type="button" className="button button--secondary export-button" onClick={exportCsv} disabled={!visibleCandidates.length}>
                                        <span aria-hidden="true">↓</span> Export CSV
                                    </button>
                                    {selected.source === 'pipeline' && (pipelineData?.batch?.state || selected.state) === 'failed' && <button type="button" className="button button--secondary export-button" onClick={retryPipelineBatch} disabled={retryingBatch}>{retryingBatch ? 'Retrying…' : 'Retry preprocessing'}</button>}
                                    <button type="button" className="button button--secondary export-button" onClick={exportXlsx} disabled={selected.source !== 'batch' || exportingXlsx}>
                                        <span aria-hidden="true">↓</span> {exportingXlsx ? 'Preparing…' : 'Export Excel'}
                                    </button>
                                </div>
                                <div className="candidate-tools">
                                    <label className="candidate-search"><span className="sr-only">Search candidates</span><span aria-hidden="true">⌕</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search by candidate name" /></label>
                                    <label className="candidate-sort"><span>Sort by</span><select value={sortBy} onChange={(event) => setSortBy(event.target.value)}>
                                        <option value="score-desc">Highest score</option><option value="score-asc">Lowest score</option><option value="name-asc">Name A–Z</option><option value="name-desc">Name Z–A</option>
                                    </select></label>
                                </div>
                                {integrationLoading && <p className="integration-muted" role="status">Updating applicant scores…</p>}
                                {visibleCandidates.length ? (
                                    <div className="candidate-table-wrap">
                                        <table className="candidate-table">
                                            <thead><tr><th scope="col">Rank</th><th scope="col">Candidate</th><th scope="col">Match score / status</th><th scope="col">Resume</th></tr></thead>
                                            <tbody>{visibleCandidates.map((candidate, index) => (
                                                <React.Fragment key={`${candidate.name}-${index}`}>
                                                <tr>
                                                    <td><span className="rank-pill">{candidate.status && candidate.status !== 'completed' ? '—' : String(index + 1).padStart(2, '0')}</span></td>
                                                    <td><strong className="candidate-name">{candidate.name || candidate.filename}</strong>{candidate.status && candidate.status !== 'completed' && <small className="candidate-status">{candidate.status}</small>}</td>
                                                    <td><span className="score-value">{candidate.status && candidate.status !== 'completed' ? candidate.status : scoreOf(candidate).toFixed(4)}</span></td>
                                                    <td className="resume-actions">
                                                        {(!candidate.status || candidate.status === 'completed') && <>
                                                            <button type="button" className="resume-link" onClick={() => previewCandidate(candidate)} disabled={previewLoading}>View</button>
                                                            <button type="button" className="resume-download" onClick={() => downloadCandidate(candidate)}>Download</button>
                                                        </>}
                                                    </td>
                                                </tr>
                                                {(!candidate.status || candidate.status === 'completed') && <tr className="analysis-table-row">
                                                    <td colSpan="4">
                                                        <details className="candidate-analysis">
                                                            <summary>Match details <span>{Math.round((candidate.analysis?.keyword_coverage || 0) * 100)}% exact term coverage</span></summary>
                                                            <p className="analysis-explainer">{candidate.score_version === 2 ? 'Match score = 75% semantic similarity across the full resume + 25% exact job-term coverage. Both components are bounded from 0 to 1; this score is not a probability or a hiring decision.' : 'This saved result uses the earlier semantic-only ranking. The value is not calibrated as a probability or hiring decision.'}</p>
                                                            <div className="analysis-grid analysis-grid--scores">
                                                                <div><strong>Overall match score</strong><p>{scoreOf(candidate).toFixed(3)} / 1.000</p></div>
                                                                <div><strong>Semantic similarity</strong><p>{Number(candidate.semantic_score ?? candidate.score ?? 0).toFixed(3)} / 1.000</p></div>
                                                            </div>
                                                            <div className="analysis-grid">
                                                                <div><strong>Terms found</strong><p>{candidate.analysis?.matched_terms?.join(', ') || 'No exact job-description terms found.'}</p></div>
                                                                <div><strong>Terms not found</strong><p>{candidate.analysis?.missing_terms?.join(', ') || 'All selected terms appear in the resume.'}</p></div>
                                                                <div><strong>Achievements and impact</strong><p>{candidate.analysis?.achievement_evidence?.join(' · ') || 'No clear evidence phrases found.'}</p></div>
                                                                <div><strong>Community and extracurricular evidence</strong><p>{candidate.analysis?.community_evidence?.join(' · ') || 'No clear evidence phrases found.'}</p></div>
                                                                <div><strong>People-skill evidence in the text</strong><p>{candidate.analysis?.people_skill_evidence?.join(' · ') || 'No clear evidence phrases found; this is not an assessment of the candidate.'}</p></div>
                                                                <div><strong>Employment dates for reviewer</strong><p>{candidate.analysis?.employment_date_evidence?.join(' · ') || 'No dates extracted. Gaps are not inferred or penalized.'}</p></div>
                                                            </div>
                                                        </details>
                                                    </td>
                                                </tr>}
                                                </React.Fragment>
                                            ))}</tbody>
                                        </table>
                                    </div>
                                ) : !integrationLoading && <p className="no-matches">{query ? `No candidate names match “${query}”.` : selected.source === 'integration' ? 'No applications have arrived for this role yet.' : 'This batch has no candidates.'}</p>}
                                {['integration', 'pipeline'].includes(selected.source) && selectedCandidateTotal > 50 && <div className="integration-pagination">
                                    <button className="button button--secondary" type="button" disabled={integrationPage === 0} onClick={() => setIntegrationPage((page) => Math.max(0, page - 1))}>Previous</button>
                                    <span>Showing {integrationPage * 50 + 1}–{Math.min((integrationPage + 1) * 50, selectedCandidateTotal)} of {selectedCandidateTotal}</span>
                                    <button className="button button--secondary" type="button" disabled={(integrationPage + 1) * 50 >= selectedCandidateTotal} onClick={() => setIntegrationPage((page) => page + 1)}>Next</button>
                                </div>}
                                <p className="score-note">Semantic similarity is not a hiring decision. Exact term coverage and extracted resume evidence are reviewer aids; employment gaps and inferred personal traits are not scored.</p>
                            </section>
                        </>
                    )}
                </main>
            </div>
            {preview && (
                <div className="resume-preview-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setPreview(null); }}>
                    <section className="resume-preview-modal" role="dialog" aria-modal="true" aria-labelledby="resume-preview-title">
                        <header><div><p className="workspace-eyebrow">RESUME PREVIEW</p><h2 id="resume-preview-title">{preview.candidate.name || preview.candidate.filename}</h2></div><button type="button" className="resume-preview-close" onClick={() => setPreview(null)} aria-label="Close resume preview">×</button></header>
                        {preview.objectUrl ? <iframe title={`Resume preview: ${preview.candidate.name}`} src={preview.objectUrl} /> : <pre className="docx-preview-text">{preview.text || 'No readable text was found in this DOCX file.'}</pre>}
                    </section>
                </div>
            )}
        </div>
    );
};

export default Results;
