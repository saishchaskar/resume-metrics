import React, { useEffect, useState } from 'react';
import api from '../api';
import { useNavigate } from 'react-router-dom';

const RESUMABLE_CHUNK_BYTES = 8 * 1024 * 1024;
const waitFor = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

const uploadZipResumably = (sessionUrl, file, onProgress) => {
    const queryUploadedBytes = () => new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        request.open('PUT', sessionUrl);
        request.setRequestHeader('Content-Range', `bytes */${file.size}`);
        request.setRequestHeader('Content-Type', 'application/zip');
        request.onload = () => {
            if (request.status === 200 || request.status === 201) return resolve(file.size);
            if (request.status !== 308) return reject(new Error('Could not resume the ZIP upload session.'));
            const range = request.getResponseHeader('Range') || '';
            const match = range.match(/bytes=0-(\d+)/i);
            resolve(match ? Number(match[1]) + 1 : 0);
        };
        request.onerror = () => reject(new Error('Could not check ZIP upload progress.'));
        request.send(new Blob());
    });

    const sendChunk = (start, end) => new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        request.open('PUT', sessionUrl);
        request.setRequestHeader('Content-Type', 'application/zip');
        request.setRequestHeader('Content-Range', `bytes ${start}-${end - 1}/${file.size}`);
        request.upload.onprogress = (event) => onProgress(Math.min(file.size, start + event.loaded));
        request.onload = () => {
            if (request.status === 200 || request.status === 201) return resolve(file.size);
            if (request.status === 308) {
                const match = (request.getResponseHeader('Range') || '').match(/bytes=0-(\d+)/i);
                const persistedOffset = match ? Number(match[1]) + 1 : 0;
                if (persistedOffset <= start) return reject(new Error('Cloud Storage has not confirmed this upload chunk yet.'));
                return resolve(Math.min(persistedOffset, file.size));
            }
            reject(new Error(`ZIP upload failed with status ${request.status}.`));
        };
        request.onerror = () => reject(new Error('ZIP upload interrupted; trying to resume…'));
        request.onabort = () => reject(new Error('ZIP upload was cancelled.'));
        request.send(file.slice(start, end));
    });

    return (async () => {
        let offset = 0;
        let retries = 0;
        while (offset < file.size) {
            const end = Math.min(offset + RESUMABLE_CHUNK_BYTES, file.size);
            try {
                offset = await sendChunk(offset, end);
                retries = 0;
                onProgress(offset);
            } catch (error) {
                if (++retries > 5) throw error;
                await waitFor(Math.min(1000 * (2 ** (retries - 1)), 10000));
                offset = await queryUploadedBytes();
                onProgress(offset);
            }
        }
    })();
};

const Dashboard = () => {
    const USE_DURABLE_BATCH_PIPELINE = process.env.NODE_ENV === 'production' || process.env.REACT_APP_BATCH_PIPELINE === '1';
    const MAX_ZIP_UPLOAD_BYTES = USE_DURABLE_BATCH_PIPELINE ? 1024 * 1024 * 1024 : 29 * 1024 * 1024;
    const ZIP_UPLOAD_LIMIT_LABEL = USE_DURABLE_BATCH_PIPELINE ? '1 GiB' : '29 MiB';
    const [jobDesc, setJobDesc] = useState('');
    const [resumes, setResumes] = useState(null);
    const [taskId, setTaskId] = useState(null);
    const [progress, setProgress] = useState({ current: 0, total: 0, filename: '' });
    const [loading, setLoading] = useState(false);
    const [rankedCandidates, setRankedCandidates] = useState([]);
    const [batchComplete, setBatchComplete] = useState(false);
    const [batchResultCount, setBatchResultCount] = useState(0);
    const [error, setError] = useState('');
    const [isDragging, setIsDragging] = useState(false);
    const [screenMode, setScreenMode] = useState('choose');
    const [integrationJobs, setIntegrationJobs] = useState([]);
    const [roleTitle, setRoleTitle] = useState('');
    const [roleExternalRef, setRoleExternalRef] = useState('');
    const [roleDescription, setRoleDescription] = useState('');
    const [roleBusy, setRoleBusy] = useState(false);
    const [roleNotice, setRoleNotice] = useState('');
    const navigate = useNavigate();

    useEffect(() => {
        if (screenMode !== 'role') return;
        api.get('/integrations/jobs')
            .then(({ data }) => setIntegrationJobs(data.jobs || []))
            .catch((requestError) => {
                setIntegrationJobs([]);
                setError(requestError.response?.data?.error || 'Could not load your portal roles.');
            });
    }, [screenMode]);

    const createIntegrationRole = async (event) => {
        event.preventDefault();
        setRoleBusy(true);
        setError('');
        setRoleNotice('');
        try {
            await api.post('/integrations/jobs', {
                title: roleTitle,
                description: roleDescription,
                external_ref: roleExternalRef || null,
            });
            setRoleTitle('');
            setRoleExternalRef('');
            setRoleDescription('');
            const { data } = await api.get('/integrations/jobs');
            setIntegrationJobs(data.jobs || []);
            setRoleNotice('Role created. Select it in Integrations to get its intake endpoint.');
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not create the job role.');
        } finally {
            setRoleBusy(false);
        }
    };

    const updateIntegrationRoleStatus = async (job) => {
        const status = job.status === 'open' ? 'closed' : 'open';
        try {
            await api.patch(`/integrations/jobs/${job.id}`, { status });
            setIntegrationJobs((current) => current.map((item) => item.id === job.id ? { ...item, status } : item));
        } catch (requestError) {
            setError(requestError.response?.data?.error || 'Could not update this role.');
        }
    };

    const chooseFile = (file) => {
        if (!file) return;
        if (file.size > MAX_ZIP_UPLOAD_BYTES) {
            setResumes(null);
            setError(`The ZIP file must be ${ZIP_UPLOAD_LIMIT_LABEL} or smaller.`);
            return;
        }
        if (!file.name.toLowerCase().endsWith('.zip')) {
            setResumes(null);
            setError('Choose a ZIP archive containing PDF or DOCX resumes.');
            return;
        }
        setResumes(file);
        setError('');
    };

    useEffect(() => {
        if (!taskId) return undefined;
        let cancelled = false;
        let timer;
        const poll = async () => {
            try {
                const { data } = await api.get(`/result/${taskId}`);
                if (cancelled) return;
                if (data.state === 'processing') {
                    setProgress(data.progress || { current: 0, total: 0, filename: '' });
                    timer = setTimeout(poll, 1500);
                } else if (data.state === 'completed') {
                    if (!data.ranked_candidates) {
                        const { data: batchData } = await api.get(`/pipeline/batches/${taskId}/candidates`, { params: { limit: 50, offset: 0 } });
                        setRankedCandidates((batchData.candidates || []).map((candidate) => ({ ...candidate, name: candidate.filename })));
                    } else {
                        setRankedCandidates(data.ranked_candidates || []);
                    }
                    setBatchResultCount((data.result_count || 0) + (data.failed_count || 0) || data.progress?.current || (data.ranked_candidates || []).length);
                    setBatchComplete(true);
                    setLoading(false);
                } else if (['uploaded', 'uploading', 'queued', 'preprocessing'].includes(data.state)) {
                    setProgress(data.progress || { current: 0, total: 0, filename: '' });
                    timer = setTimeout(poll, 1500);
                } else {
                    setError(data.error || 'An error occurred during processing.');
                    setLoading(false);
                }
            } catch (requestError) {
                if (!cancelled) {
                    setError(requestError.response?.data?.error || 'Could not check processing status. Please try again.');
                    setLoading(false);
                }
            }
        };
        poll();
        return () => { cancelled = true; clearTimeout(timer); };
    }, [taskId]);

    const handleSubmit = async (event) => {
        event.preventDefault();
        if (!jobDesc.trim()) {
            setError('Add the job description before starting the review.');
            return;
        }
        if (!resumes) {
            setError('Add a ZIP archive containing PDF or DOCX resumes.');
            return;
        }

        setLoading(true);
        setError('');
        setRankedCandidates([]);
        setBatchComplete(false);
        setBatchResultCount(0);
        setTaskId(null);
        setProgress({ current: 0, total: 0, filename: '' });

        try {
            let data;
            if (USE_DURABLE_BATCH_PIPELINE) {
                const created = await api.post('/pipeline/batches', {
                    job_description: jobDesc.trim(),
                    filename: resumes.name,
                    byte_size: resumes.size,
                });
                await uploadZipResumably(created.data.upload_url, resumes, (uploadedBytes) => {
                    setProgress({ current: uploadedBytes, total: resumes.size, filename: `Uploading ${resumes.name}` });
                });
                setProgress({ current: 0, total: 0, filename: 'Submitting ZIP for preprocessing' });
                const started = await api.post(`/pipeline/batches/${created.data.batch_id}/start`);
                data = { task_id: started.data.batch_id };
            } else {
                const formData = new FormData();
                formData.append('job_description', jobDesc.trim());
                formData.append('resumes_zip', resumes);
                const response = await api.post('/process', formData, {
                    headers: { 'Content-Type': 'multipart/form-data' },
                });
                data = response.data;
            }
            if (!data.task_id) throw new Error('The server did not return a processing task.');
            setTaskId(data.task_id);
        } catch (requestError) {
            setError(requestError.response?.data?.error || requestError.message || 'Could not start resume processing.');
            setLoading(false);
        }
    };

    const progressPercent = progress.total ? Math.min((progress.current / progress.total) * 100, 100) : 0;

    return (
        <div className="workspace-page">
            <div className="workspace-heading">
                <div>
                    <p className="workspace-eyebrow">RECRUITER WORKSPACE</p>
                    <h1>{screenMode === 'manual' ? 'Manual ZIP screening' : screenMode === 'role' ? 'Create a portal role' : 'Choose a screening method'}</h1>
                    <p>{screenMode === 'manual' ? 'Add a role brief and upload one ZIP of resumes to rank.' : screenMode === 'role' ? 'Set up a role here, then connect your careers portal once in Integrations.' : 'Start with a resume batch or connect your careers portal for applicant-by-applicant ranking.'}</p>
                </div>
                {screenMode !== 'choose' ? (
                    <button type="button" className="button button--secondary" onClick={() => { setScreenMode('choose'); setError(''); }}>← Screening options</button>
                ) : (
                    <button type="button" className="button button--secondary" onClick={() => navigate('/results')}>
                        View all results <span aria-hidden="true">↗</span>
                    </button>
                )}
            </div>

            {screenMode === 'choose' && <section className="workspace-mode-chooser" aria-label="Choose how to screen resumes">
                <button type="button" className="workspace-mode-card workspace-mode-card--manual" onClick={() => setScreenMode('manual')}>
                    <span className="workspace-mode-card__icon" aria-hidden="true">↑</span>
                    <span className="workspace-mode-card__copy"><strong>Manual ZIP upload</strong><small>Upload a resume batch and rank it against a job description.</small></span>
                    <span className="workspace-mode-card__action">Set up a batch →</span>
                </button>
                <button type="button" className="workspace-mode-card workspace-mode-card--integration" onClick={() => { setError(''); setScreenMode('role'); }}>
                    <span className="workspace-mode-card__icon" aria-hidden="true">↗</span>
                    <span className="workspace-mode-card__copy"><strong>Careers portal integration</strong><small>Create a role and receive applicants one at a time from your ATS or careers site.</small></span>
                    <span className="workspace-mode-card__action">Set up a role →</span>
                </button>
            </section>}

            {screenMode === 'role' && <div className="role-setup-layout">
                <section className="workspace-card role-setup-card">
                    <div className="workspace-card__heading"><div className="step-marker">01</div><div><h2>Create a job role</h2><p>Your portal will submit applicants against this role.</p></div></div>
                    {roleNotice && <div className="integration-notice" role="status">{roleNotice}</div>}
                    <form className="integration-form" onSubmit={createIntegrationRole}>
                        <label>Role title<input value={roleTitle} onChange={(event) => setRoleTitle(event.target.value)} maxLength={160} required placeholder="Senior product analyst" /></label>
                        <label>External role reference <span>Optional ATS requisition ID</span><input value={roleExternalRef} onChange={(event) => setRoleExternalRef(event.target.value)} maxLength={200} placeholder="REQ-1042" /></label>
                        <label>Job description<textarea value={roleDescription} onChange={(event) => setRoleDescription(event.target.value)} maxLength={20000} rows={7} required placeholder="Role responsibilities, skills, and requirements…" /></label>
                        {error && <div className="workspace-alert" role="alert">{error}</div>}
                        <button className="button button--primary" type="submit" disabled={roleBusy}>{roleBusy ? 'Creating role…' : 'Create role'}</button>
                    </form>
                    <div className="integration-list">
                        <div className="role-list-heading"><h3>Your portal roles</h3><button className="text-button" type="button" onClick={() => navigate('/integrations')}>API settings →</button></div>
                        {integrationJobs.length === 0 ? <p className="integration-muted">No portal roles yet. Create one to begin collecting applicants.</p> : integrationJobs.map((job) => <div className="integration-role-row" key={job.id}>
                            <div className="integration-role"><span><strong>{job.title}</strong><small>{job.external_ref || 'No external reference'} · {job.applicant_count || 0} applicants · {job.status || 'open'}</small></span><code>{job.id.slice(0, 8)}</code></div>
                            <button type="button" className="text-button integration-role-toggle" onClick={() => updateIntegrationRoleStatus(job)}>{job.status === 'open' ? 'Close' : 'Reopen'}</button>
                        </div>)}
                    </div>
                    {integrationJobs.length > 0 && <button type="button" className="button button--secondary role-results-link" onClick={() => navigate('/results')}>View role results →</button>}
                </section>
                <aside className="role-setup-aside"><span className="workspace-eyebrow">HOW IT CONNECTS</span><h2>Create the role once. Reuse the portal connection.</h2><p>Each role gets its own ID and ranked applicant list. Your API key and portal endpoint instructions stay in Integrations and can be reused across roles.</p><button type="button" className="button button--secondary" onClick={() => navigate('/integrations')}>Open integration settings →</button></aside>
            </div>}

            {screenMode === 'manual' && <div className="workspace-layout">
                <section id="manual-zip-screening" className="workspace-card" aria-labelledby="screening-title">
                    <div className="workspace-card__heading">
                        <div className="step-marker">01</div>
                        <div>
                            <h2 id="screening-title">Set up a screening</h2>
                            <p>Add the role details and a ZIP of resumes to rank.</p>
                        </div>
                    </div>

                    <form onSubmit={handleSubmit} className="screening-form">
                        <div className="form-field">
                            <div className="form-field__heading">
                                <label htmlFor="jobDescription">Job description</label>
                                <span>{jobDesc.length} characters</span>
                            </div>
                            <textarea
                                id="jobDescription"
                                value={jobDesc}
                                onChange={(event) => setJobDesc(event.target.value)}
                                placeholder="Paste the responsibilities, skills, and experience required for this role…"
                                rows={8}
                                required
                                disabled={loading}
                            />
                            <p className="field-hint">Include the key skills you want the ranking to consider.</p>
                        </div>

                        <div className="form-field">
                            <div className="form-field__heading">
                                <label htmlFor="resumeZip">Resume files</label>
                            <span>ZIP · PDF or DOCX · up to {ZIP_UPLOAD_LIMIT_LABEL}</span>
                            </div>
                            <label
                                htmlFor="resumeZip"
                                className={`upload-dropzone${isDragging ? ' is-dragging' : ''}${resumes ? ' has-file' : ''}`}
                                onDragOver={(event) => { event.preventDefault(); setIsDragging(true); }}
                                onDragLeave={() => setIsDragging(false)}
                                onDrop={(event) => {
                                    event.preventDefault();
                                    setIsDragging(false);
                                    chooseFile(event.dataTransfer.files?.[0]);
                                }}
                            >
                                <span className="upload-icon" aria-hidden="true">
                                    <svg viewBox="0 0 24 24"><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5M5 14.5v4A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5v-4" /></svg>
                                </span>
                                {resumes ? (
                                    <>
                                        <strong>{resumes.name}</strong>
                                        <span>{(resumes.size / (1024 * 1024)).toFixed(2)} MB · Ready to upload</span>
                                    </>
                                ) : (
                                    <>
                                        <strong>Drop your resume ZIP here</strong>
                                        <span>or <em>browse files</em> on your computer</span>
                                        <small>No fixed resume-count cap · 10 MiB each · 10 GiB expanded · {ZIP_UPLOAD_LIMIT_LABEL} ZIP maximum</small>
                                    </>
                                )}
                                <input
                                    id="resumeZip"
                                    type="file"
                                    accept=".zip,application/zip"
                                    aria-label="Choose resume ZIP file"
                                    onChange={(event) => {
                                        chooseFile(event.target.files?.[0]);
                                        event.target.value = '';
                                    }}
                                    disabled={loading}
                                />
                            </label>
                            {resumes && <button className="text-button clear-file" type="button" onClick={() => setResumes(null)} disabled={loading}>Remove selected file</button>}
                        </div>

                        {error && <div className="workspace-alert" role="alert">{error}</div>}

                        <button className="button button--primary start-screening" type="submit" disabled={loading}>
                            {loading ? 'Screening in progress…' : 'Start screening'}
                            {!loading && <span aria-hidden="true">→</span>}
                        </button>
                    </form>

                    {loading && (
                        <div className="processing-panel" role="status" aria-live="polite">
                            <div className="processing-panel__top">
                                <span className="processing-pulse" />
                                <div><strong>{progress.filename?.startsWith('Uploading ') ? 'Uploading ZIP to secure storage' : 'Reviewing resumes'}</strong><span>{progress.filename?.startsWith('Uploading ') && progress.total ? `${(progress.current / (1024 * 1024)).toFixed(1)} of ${(progress.total / (1024 * 1024)).toFixed(1)} MiB uploaded` : progress.total ? `${progress.current} of ${progress.total} processed` : 'Preparing your batch…'}</span></div>
                                <strong>{Math.round(progressPercent)}%</strong>
                            </div>
                            <div className="progress-track"><span style={{ width: `${progressPercent}%` }} /></div>
                            {progress.filename && <p>Current file: <strong>{progress.filename}</strong></p>}
                        </div>
                    )}

                    {!loading && (rankedCandidates.length > 0 || batchComplete) && (
                        <div className="completion-panel" role="status">
                            <div><span className="completion-check">✓</span><div><strong>Screening complete</strong><span>{batchResultCount || rankedCandidates.length} candidates processed; results are saved in shared storage.</span></div></div>
                            <button type="button" className="button button--secondary" onClick={() => navigate('/results')}>Review ranked candidates →</button>
                        </div>
                    )}
                </section>

                <aside className="workspace-aside">
                    <div className="aside-card aside-card--dark">
                        <p className="workspace-eyebrow">YOUR WORKFLOW</p>
                        <h2>From a role brief to a ranked shortlist.</h2>
                        <ol className="workflow-steps">
                            <li><span>1</span><div><strong>Describe the role</strong><p>Paste the job description and core requirements.</p></div></li>
                            <li><span>2</span><div><strong>Add resumes</strong><p>Upload a ZIP containing PDF or DOCX files.</p></div></li>
                            <li><span>3</span><div><strong>Review matches</strong><p>Search, sort, and open ranked candidate files.</p></div></li>
                        </ol>
                    </div>
                    <div className="aside-card aside-card--note">
                        <span className="note-icon" aria-hidden="true">i</span>
                        <div><strong>How matching works</strong><p>Semantic similarity compares the full resume with the role brief. Results also show exact term coverage and resume evidence separately; gaps and inferred traits do not affect rank.</p></div>
                    </div>
                </aside>
            </div>}
        </div>
    );
};

export default Dashboard;
