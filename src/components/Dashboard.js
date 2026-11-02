import React, { useEffect, useState } from 'react';
import api from '../api';
import { useNavigate } from 'react-router-dom';

const Dashboard = () => {
    const [jobDesc, setJobDesc] = useState('');
    const [resumes, setResumes] = useState(null);
    const [taskId, setTaskId] = useState(null);
    const [progress, setProgress] = useState({ current: 0, total: 0, filename: '' });
    const [loading, setLoading] = useState(false);
    const [rankedCandidates, setRankedCandidates] = useState([]);
    const [error, setError] = useState('');
    const [isDragging, setIsDragging] = useState(false);
    const navigate = useNavigate();

    const chooseFile = (file) => {
        if (!file) return;
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
                    setRankedCandidates(data.ranked_candidates || []);
                    setLoading(false);
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
        setTaskId(null);
        setProgress({ current: 0, total: 0, filename: '' });

        const formData = new FormData();
        formData.append('job_description', jobDesc.trim());
        formData.append('resumes_zip', resumes);
        try {
            const { data } = await api.post('/process', formData, {
                headers: { 'Content-Type': 'multipart/form-data' },
            });
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
                    <h1>Resume screening</h1>
                    <p>Compare candidate experience against a role and review the strongest matches.</p>
                </div>
                <button type="button" className="button button--secondary" onClick={() => navigate('/results')}>
                    View saved results <span aria-hidden="true">↗</span>
                </button>
            </div>

            <div className="workspace-layout">
                <section className="workspace-card" aria-labelledby="screening-title">
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
                                <span>ZIP · PDF or DOCX</span>
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
                                        <small>One ZIP archive with PDF or DOCX resumes</small>
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
                                <div><strong>Reviewing resumes</strong><span>{progress.total ? `${progress.current} of ${progress.total} processed` : 'Preparing your batch…'}</span></div>
                                <strong>{Math.round(progressPercent)}%</strong>
                            </div>
                            <div className="progress-track"><span style={{ width: `${progressPercent}%` }} /></div>
                            {progress.filename && <p>Current file: <strong>{progress.filename}</strong></p>}
                        </div>
                    )}

                    {!loading && rankedCandidates.length > 0 && (
                        <div className="completion-panel" role="status">
                            <div><span className="completion-check">✓</span><div><strong>Screening complete</strong><span>{rankedCandidates.length} candidates ranked and saved.</span></div></div>
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
            </div>
        </div>
    );
};

export default Dashboard;
