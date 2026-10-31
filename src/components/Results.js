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
            ['Rank', 'Candidate', 'Similarity score', 'Resume URL'],
            ...visibleCandidates.map((candidate, index) => [
                index + 1,
                safeText(candidate.name),
                scoreOf(candidate).toFixed(6),
                safeText(candidate.url),
            ]),
        ];
        const csv = rows.map((row) => row.join(',')).join('\r\n');
        const blobUrl = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
        const link = document.createElement('a');
        link.href = blobUrl;
        link.download = `resumesignal-results-${selectedId}.csv`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
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
                                <article className="summary-card"><span>Top similarity</span><strong>{topScore.toFixed(3)}</strong><small>Highest candidate score</small></article>
                                <article className="summary-card"><span>Average similarity</span><strong>{averageScore.toFixed(3)}</strong><small>Across this batch</small></article>
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
                                            <thead><tr><th scope="col">Rank</th><th scope="col">Candidate</th><th scope="col">Similarity score</th><th scope="col">Resume</th></tr></thead>
                                            <tbody>{visibleCandidates.map((candidate, index) => (
                                                <tr key={`${candidate.name}-${index}`}>
                                                    <td><span className="rank-pill">{String(index + 1).padStart(2, '0')}</span></td>
                                                    <td><strong className="candidate-name">{candidate.name}</strong></td>
                                                    <td><span className="score-value">{scoreOf(candidate).toFixed(4)}</span></td>
                                                    <td>{candidate.url ? <a href={candidate.url} target="_blank" rel="noopener noreferrer" className="resume-link">Open resume <span aria-hidden="true">↗</span></a> : <span className="muted-cell">Unavailable</span>}</td>
                                                </tr>
                                            ))}</tbody>
                                        </table>
                                    </div>
                                ) : <p className="no-matches">{query ? `No candidate names match “${query}”.` : 'This batch has no candidates.'}</p>}
                                <p className="score-note">Similarity scores help prioritize review; use them alongside your hiring criteria.</p>
                            </section>
                        </>
                    )}
                </main>
            </div>
        </div>
    );
};

export default Results;
