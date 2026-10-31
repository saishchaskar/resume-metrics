import React from 'react';

const AuthLayout = ({ children }) => (
    <section className="auth-layout">
        <aside className="auth-aside" aria-label="Resume screening workspace">
            <p className="auth-kicker">SCREENING WORKSPACE</p>
            <h2>Find the signal in every resume.</h2>
            <p className="auth-aside-copy">
                Bring job requirements and candidate experience into one clear, focused view.
            </p>
            <div className="signal-preview" aria-label="Example candidate match scores">
                <div className="signal-preview__header">
                    <span>Match overview</span>
                    <span className="signal-preview__status"><i /> READY</span>
                </div>
                <div className="signal-row">
                    <span className="signal-row__rank">01</span>
                    <span className="signal-row__name">Product analyst</span>
                    <span className="signal-row__score">92%</span>
                    <span className="signal-row__bar"><i style={{ width: '92%' }} /></span>
                </div>
                <div className="signal-row">
                    <span className="signal-row__rank">02</span>
                    <span className="signal-row__name">Data specialist</span>
                    <span className="signal-row__score">84%</span>
                    <span className="signal-row__bar"><i style={{ width: '84%' }} /></span>
                </div>
                <div className="signal-row">
                    <span className="signal-row__rank">03</span>
                    <span className="signal-row__name">Research associate</span>
                    <span className="signal-row__score">76%</span>
                    <span className="signal-row__bar"><i style={{ width: '76%' }} /></span>
                </div>
                <p className="signal-preview__caption">SAMPLE MATCH RESULTS</p>
            </div>
            <div className="auth-aside-foot"><span>01</span><span>ResumeMetrics</span></div>
        </aside>
        <div className="auth-panel">
            <div className="auth-form-content">{children}</div>
        </div>
    </section>
);

export default AuthLayout;
