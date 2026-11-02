import React, { useCallback, useEffect, useState } from 'react';
import api, { apiOrigin } from '../api';

const Integrations = () => {
    const [jobs, setJobs] = useState([]);
    const [keys, setKeys] = useState([]);
    const [label, setLabel] = useState('Careers portal');
    const [newKey, setNewKey] = useState('');
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [busy, setBusy] = useState(false);
    const [selectedJobId, setSelectedJobId] = useState('');
    const apiRoot = `${apiOrigin || 'http://localhost:5000'}/api/v1`;

    const load = useCallback(async () => {
        try {
            const [keyResponse, jobResponse] = await Promise.all([
                api.get('/integrations/keys'),
                api.get('/integrations/jobs'),
            ]);
            setKeys(keyResponse.data.keys || []);
            const nextJobs = jobResponse.data.jobs || [];
            setJobs(nextJobs);
            setSelectedJobId((current) => nextJobs.some((job) => job.id === current) ? current : (nextJobs[0]?.id || ''));
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not load integration settings.');
        }
    }, []);

    useEffect(() => { load(); }, [load]);

    const createKey = async (event) => {
        event.preventDefault();
        setBusy(true); setError(''); setNotice(''); setNewKey('');
        try {
            const { data } = await api.post('/integrations/keys', { label });
            setNewKey(data.api_key);
            setNotice('Copy this key now. It is shown only once.');
            await load();
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not create the API key.');
        } finally { setBusy(false); }
    };

    const revokeKey = async (keyId) => {
        setBusy(true); setError(''); setNotice('');
        try {
            await api.delete(`/integrations/keys/${keyId}`);
            setNotice('API key revoked.');
            await load();
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not revoke the API key.');
        } finally { setBusy(false); }
    };

    const copy = async (value) => {
        try { await navigator.clipboard.writeText(value); setNotice('Copied to clipboard.'); }
        catch (_error) { setNotice('Select and copy the text manually.'); }
    };

    const jobId = selectedJobId || 'YOUR_JOB_ID';
    const curl = `curl -X POST '${apiRoot}/jobs/${jobId}/applications' -H 'Authorization: Bearer $RESUMEMETRICS_API_KEY' -F 'candidate_name=Alex Candidate' -F 'candidate_email=alex@example.com' -F 'resume=@./resume.pdf'`;

    return (
        <div className="integrations-page">
            <div className="workspace-heading">
                <div>
                    <p className="workspace-eyebrow">WORKSPACE SETTINGS</p>
                    <h1>Portal connection settings</h1>
                    <p>Create and manage your API access once, then reuse it for every role.</p>
                </div>
            </div>

            {error && <div className="workspace-alert" role="alert">{error}</div>}
            {notice && <div className="integration-notice" role="status">{notice}</div>}

            <div className="integration-settings-grid">
                    <section className="workspace-card">
                        <div className="workspace-card__heading"><div className="step-marker">01</div><div><h2>API access</h2><p>Use a private key from your server. Never put it in browser code.</p></div></div>
                        <form className="integration-key-form" onSubmit={createKey}>
                            <label>Key label<input value={label} onChange={(event) => setLabel(event.target.value)} maxLength={80} required /></label>
                            <button className="button button--primary" type="submit" disabled={busy}>Create API key</button>
                        </form>
                        {newKey && <div className="integration-secret"><strong>Copy your key now</strong><code>{newKey}</code><button className="button button--secondary" type="button" onClick={() => copy(newKey)}>Copy key</button></div>}
                        <div className="integration-list">
                            <h3>API keys</h3>
                            {keys.length === 0 ? <p className="integration-muted">No API keys created.</p> : keys.map((key) => (
                                <div className="integration-key-row" key={key.id}>
                                    <span><strong>{key.label}</strong><small>{key.prefix}… · {key.revoked_at ? 'Revoked' : 'Active'}</small></span>
                                    {!key.revoked_at && <button type="button" className="text-button" onClick={() => revokeKey(key.id)} disabled={busy}>Revoke</button>}
                                </div>
                            ))}
                        </div>
                    </section>

                    <section className="workspace-card integration-docs">
                        <div className="workspace-card__heading"><div className="step-marker">02</div><div><h2>Connect your portal backend</h2><p>Your company server or ATS sends applicant data to the ResumeMetrics Flask API.</p></div></div>
                        <label className="integration-job-picker">Role endpoint
                            <select value={selectedJobId} onChange={(event) => setSelectedJobId(event.target.value)} disabled={!jobs.length}>
                                {jobs.length ? jobs.map((job) => <option key={job.id} value={job.id}>{job.title} · {job.status || 'open'}</option>) : <option value="">Create a role in Workspace first</option>}
                            </select>
                        </label>
                        <div className="integration-docs__endpoint"><span>Applicant intake endpoint</span><code>POST {apiRoot}/jobs/{jobId}/applications</code></div>
                        <pre><code>{curl}</code></pre>
                        <button type="button" className="button button--secondary" onClick={() => copy(curl)}>Copy example request</button>
                        <p className="integration-muted">Call this endpoint from your trusted company server or ATS. Keep the API key there; never place it in the public careers page or applicant browser. The API returns an application ID and <code>queued</code> status. Recruiters review applicants alongside ZIP batch results on the Results page. Fetch results with <code>GET {apiRoot}/jobs/{jobId}/applications?limit=50&amp;offset=0</code>. Vendor-specific OAuth connectors can be added later; this REST API works with systems that support outbound webhooks or API calls.</p>
                    </section>
            </div>
        </div>
    );
};

export default Integrations;
