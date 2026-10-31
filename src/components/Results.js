import React, { useCallback, useEffect, useState } from 'react';
import api from '../api';

const Results = () => {
    const [results, setResults] = useState([]);
    const [selectedId, setSelectedId] = useState(null);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);

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

    const removeResult = async (id) => {
        try {
            await api.delete(`/results/${id}`);
            const remaining = results.filter((result) => result.id !== id);
            setResults(remaining);
            if (selectedId === id) setSelectedId(remaining[0]?.id ?? null);
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not delete this result.');
        }
    };

    const selected = results.find((result) => result.id === selectedId);

    return (
        <div className="flex min-h-screen flex-col md:flex-row">
            <aside className="w-full bg-gray-100 p-4 md:w-1/4" aria-label="Saved results">
                <h2 className="mb-4 text-lg font-bold">Previous Results</h2>
                {loading && <p role="status">Loading results…</p>}
                {!loading && results.length === 0 && <p>No saved results yet.</p>}
                <ul>
                    {results.map((result) => (
                        <li key={result.id} className={`mb-2 flex items-center justify-between gap-2 rounded p-2 ${selectedId === result.id ? 'bg-gray-300' : 'bg-white'}`}>
                            <button type="button" className="min-w-0 flex-1 text-left" onClick={() => setSelectedId(result.id)}>
                                {new Date(result.timestamp).toLocaleString()} · {(result.candidates || []).length} candidates
                            </button>
                            <button type="button" className="text-red-600 hover:text-red-800" aria-label={`Delete result ${result.id}`} onClick={() => removeResult(result.id)}>×</button>
                        </li>
                    ))}
                </ul>
            </aside>
            <section className="min-w-0 flex-1 overflow-x-auto p-4">
                <h1 className="mb-4 text-2xl font-bold">Ranked Candidates</h1>
                {error && <p className="mb-4 rounded bg-red-100 p-3 text-red-700" role="alert">{error}</p>}
                {selected?.candidates?.length ? (
                    <table className="min-w-full border border-gray-200 bg-white">
                        <thead><tr className="bg-gray-800 text-white"><th className="px-4 py-2">Rank</th><th className="px-4 py-2">Candidate Name</th><th className="px-4 py-2">Similarity Score</th><th className="px-4 py-2">Resume</th></tr></thead>
                        <tbody>{selected.candidates.map((candidate, index) => (
                            <tr key={`${candidate.name}-${index}`} className="text-center hover:bg-gray-100">
                                <td className="border-b px-4 py-2">{index + 1}</td>
                                <td className="border-b px-4 py-2">{candidate.name}</td>
                                <td className="border-b px-4 py-2">{Number(candidate.score || 0).toFixed(4)}</td>
                                <td className="border-b px-4 py-2">{candidate.url ? <a href={candidate.url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">View Resume</a> : 'No resume available'}</td>
                            </tr>
                        ))}</tbody>
                    </table>
                ) : !loading && <p>{selected ? 'No candidates in this result.' : 'Select a saved result to view candidates.'}</p>}
            </section>
        </div>
    );
};

export default Results;
