import React, { useCallback, useEffect, useMemo, useState } from 'react';
import api from '../api';

const scoreOf = (candidate) => Number(candidate.score) || 0;

const Results = () => {
    const [results, setResults] = useState([]);
    const [selectedId, setSelectedId] = useState(null);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);
    const [query, setQuery] = useState('');
    const [sortBy, setSortBy] = useState('score-desc');
    const [deletingId, setDeletingId] = useState(null);
    const [preview, setPreview] = useState(null);
    const [previewLoading, setPreviewLoading] = useState(false);

    useEffect(() => () => {
        if (preview?.objectUrl) URL.revokeObjectURL(preview.objectUrl);
    }, [preview]);

    const loadResults = useCallback(async () => {
        setLoading(true);
        try {
            const { data } = await api.get('/results');
            const entries = data.results || [];
            setResults(entries);
            setSelectedId((current) => entries.some((entry) => entry.id === current) ? current : (entries[0]?.id ?? null));
            setError('');
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not load saved results.');
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { loadResults(); }, [loadResults]);

    const selected = results.find((result) => result.id === selectedId);
    const candidates = useMemo(() => selected?.candidates || [], [selected]);
    const visibleCandidates = useMemo(() => {
        const filtered = candidates.filter((candidate) => (candidate.name || '').toLowerCase().includes(query.trim().toLowerCase()));
        return [...filtered].sort((left, right) => {
            if (sortBy === 'score-asc') return scoreOf(left) - scoreOf(right);
            if (sortBy === 'name-asc') return (left.name || '').localeCompare(right.name || '');
            if (sortBy === 'name-desc') return (right.name || '').localeCompare(left.name || '');
            return scoreOf(right) - scoreOf(left);
        });
    }, [candidates, query, sortBy]);

    const averageScore = candidates.length
        ? candidates.reduce((sum, candidate) => sum + scoreOf(candidate), 0) / candidates.length
        : 0;
    const topScore = candidates.length
        ? candidates.reduce((top, candidate) => Math.max(top, scoreOf(candidate)), Number.NEGATIVE_INFINITY)
        : 0;

    const removeResult = async (id) => {
        setDeletingId(id);
        try {
            await api.delete(`/results/${id}`);
            const remaining = results.filter((result) => result.id !== id);
            setResults(remaining);
            if (selectedId === id) setSelectedId(remaining[0]?.id ?? null);
            setError('');
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not delete this result.');
        } finally {
            setDeletingId(null);
        }
    };

    const exportCsv = () => {
        const safeText = (value) => {
            const text = String(value ?? '');
            const formulaSafe = /^[=+@-]/.test(text) ? `'${text}` : text;
            return `"${formulaSafe.replace(/"/g, '""')}"`;
        };
        const rows = [
            ['Rank', 'Candidate', 'Semantic similarity', 'Exact keyword coverage'],
            ...visibleCandidates.map((candidate, index) => [
                index + 1,
                safeText(candidate.name),
                scoreOf(candidate).toFixed(6),
                `${((candidate.analysis?.keyword_coverage || 0) * 100).toFixed(1)}%`,
            ]),
        ];
        const csv = rows.map((row) => row.join(',')).join('\r\n');
        const blobUrl = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
        const link = document.createElement('a');
        link.href = blobUrl;
        link.download = `resumemetrics-results-${selectedId}.csv`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    };

    const previewCandidate = async (candidate) => {
        setPreviewLoading(true);
        setError('');
        try {
            const objectName = candidate.object_name || candidate.name;
            const encodedName = encodeURIComponent(objectName);
            if (candidate.name?.toLowerCase().endsWith('.docx')) {
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
            const filename = encodeURIComponent(candidate.name || 'resume');
            const { data } = await api.get(`/resume-preview/${encodeURIComponent(objectName)}?download=1&filename=${filename}`, { responseType: 'blob' });
            const objectUrl = URL.createObjectURL(data);
            const link = document.createElement('a');
            link.href = objectUrl;
            link.download = candidate.name || 'resume';
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
                    <p>Review saved screening batches, find candidates, and export a shortlist.</p>
                </div>
                <button type="button" className="button button--secondary" onClick={loadResults} disabled={loading}>
                    <span aria-hidden="true">↻</span> {loading ? 'Refreshing…' : 'Refresh results'}
                </button>
            </header>

            {error && <div className="workspace-alert results-alert" role="alert">{error}</div>}

            <div className="results-layout">
                <aside className="history-card" aria-label="Saved screening batches">
                    <div className="history-card__header"><div><h2>Saved batches</h2><p>Your completed screenings</p></div><span className="history-count">{results.length}</span></div>
                    {loading && results.length === 0 && <p className="empty-history" role="status">Loading batches…</p>}
                    {!loading && results.length === 0 && <div className="empty-history"><span aria-hidden="true">▤</span><strong>No screenings yet</strong><p>Your completed batches will appear here.</p></div>}
                    <ul className="history-list">
                        {results.map((result) => (
                            <li key={result.id} className={`history-item${selectedId === result.id ? ' is-selected' : ''}`}>
                                <button type="button" className="history-item__select" onClick={() => { setSelectedId(result.id); setQuery(''); }} aria-current={selectedId === result.id ? 'true' : undefined}>
                                    <span className="history-item__date">{new Date(result.timestamp).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}</span>
                                    <strong>{(result.candidates || []).length} candidates</strong>
                                    <span>{new Date(result.timestamp).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}</span>
                                </button>
                                <button type="button" className="history-item__delete" aria-label={`Delete screening ${result.id}`} title="Delete screening" disabled={deletingId === result.id} onClick={() => removeResult(result.id)}>
                                    {deletingId === result.id ? '…' : '×'}
                                </button>
                            </li>
                        ))}
                    </ul>
                </aside>

                <main className="results-main">
                    {!selected ? (
                        <section className="results-empty"><span aria-hidden="true">✳</span><h2>Your shortlist starts here</h2><p>Run a screening to see ranked candidates, compare similarity scores, and export your results.</p></section>
                    ) : (
                        <>
                            <div className="results-summary">
                                <article className="summary-card"><span>Ranked candidates</span><strong>{candidates.length}</strong><small>In this screening</small></article>
                                <article className="summary-card"><span>Top semantic similarity</span><strong>{topScore.toFixed(3)}</strong><small>Highest candidate score</small></article>
                                <article className="summary-card"><span>Average semantic similarity</span><strong>{averageScore.toFixed(3)}</strong><small>Across this batch</small></article>
                            </div>

                            <section className="candidate-card" aria-labelledby="candidate-list-title">
                                <div className="candidate-card__header">
                                    <div><p className="workspace-eyebrow">BATCH REVIEW</p><h2 id="candidate-list-title">Candidate list</h2><p>Screened {new Date(selected.timestamp).toLocaleString()}</p></div>
                                    <button type="button" className="button button--secondary export-button" onClick={exportCsv} disabled={!visibleCandidates.length}>
                                        <span aria-hidden="true">↓</span> Export CSV
                                    </button>
                                </div>
                                <div className="candidate-tools">
                                    <label className="candidate-search"><span className="sr-only">Search candidates</span><span aria-hidden="true">⌕</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search by candidate name" /></label>
                                    <label className="candidate-sort"><span>Sort by</span><select value={sortBy} onChange={(event) => setSortBy(event.target.value)}>
                                        <option value="score-desc">Highest score</option><option value="score-asc">Lowest score</option><option value="name-asc">Name A–Z</option><option value="name-desc">Name Z–A</option>
                                    </select></label>
                                </div>
                                {visibleCandidates.length ? (
                                    <div className="candidate-table-wrap">
                                        <table className="candidate-table">
                                            <thead><tr><th scope="col">Rank</th><th scope="col">Candidate</th><th scope="col">Semantic score</th><th scope="col">Resume</th></tr></thead>
                                            <tbody>{visibleCandidates.map((candidate, index) => (
                                                <React.Fragment key={`${candidate.name}-${index}`}>
                                                <tr>
                                                    <td><span className="rank-pill">{String(index + 1).padStart(2, '0')}</span></td>
                                                    <td><strong className="candidate-name">{candidate.name}</strong></td>
                                                    <td><span className="score-value">{scoreOf(candidate).toFixed(4)}</span></td>
                                                    <td className="resume-actions">
                                                        <button type="button" className="resume-link" onClick={() => previewCandidate(candidate)} disabled={previewLoading}>View</button>
                                                        <button type="button" className="resume-download" onClick={() => downloadCandidate(candidate)}>Download</button>
                                                    </td>
                                                </tr>
                                                <tr className="analysis-table-row">
                                                    <td colSpan="4">
                                                        <details className="candidate-analysis">
                                                            <summary>Match details <span>{Math.round((candidate.analysis?.keyword_coverage || 0) * 100)}% exact term coverage</span></summary>
                                                            <p className="analysis-explainer">Rank is based on semantic similarity across the full resume. Exact term coverage is shown separately and does not change rank. Review the resume before making decisions.</p>
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
                                                </tr>
                                                </React.Fragment>
                                            ))}</tbody>
                                        </table>
                                    </div>
                                ) : <p className="no-matches">{query ? `No candidate names match “${query}”.` : 'This batch has no candidates.'}</p>}
                                <p className="score-note">Semantic similarity is not a hiring decision. Exact term coverage and extracted resume evidence are reviewer aids; employment gaps and inferred personal traits are not scored.</p>
                            </section>
                        </>
                    )}
                </main>
            </div>
            {preview && (
                <div className="resume-preview-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setPreview(null); }}>
                    <section className="resume-preview-modal" role="dialog" aria-modal="true" aria-labelledby="resume-preview-title">
                        <header><div><p className="workspace-eyebrow">RESUME PREVIEW</p><h2 id="resume-preview-title">{preview.candidate.name}</h2></div><button type="button" className="resume-preview-close" onClick={() => setPreview(null)} aria-label="Close resume preview">×</button></header>
                        {preview.objectUrl ? <iframe title={`Resume preview: ${preview.candidate.name}`} src={preview.objectUrl} /> : <pre className="docx-preview-text">{preview.text || 'No readable text was found in this DOCX file.'}</pre>}
                    </section>
                </div>
            )}
        </div>
    );
};

export default Results;
