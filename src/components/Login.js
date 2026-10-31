// src/components/Login.js
import React, { useState, useContext } from 'react';
import { AuthContext } from '../context/AuthContext';
import { useNavigate, Link } from 'react-router-dom';
import AuthLayout from './AuthLayout';

const Login = () => {
    const { login } = useContext(AuthContext);
    const navigate = useNavigate();
    const [credentials, setCredentials] = useState({
        username: '',
        password: '',
    });
    const [error, setError] = useState('');
    const [isSubmitting, setIsSubmitting] = useState(false);

    const handleChange = (e) => {
        const { name, value } = e.target;
        setCredentials((prev) => ({ ...prev, [name]: value }));
    };



    const handleSubmit = async (e) => {
        e.preventDefault();
        setError('');
        setIsSubmitting(true);
        try {
            const result = await login(credentials);
            if (result.success) {
                navigate('/dashboard');
            } else {
                setError(result.message);
            }
        } finally {
            setIsSubmitting(false);
        }
    };

    return (
        <AuthLayout>
            <h1 className="auth-title">Welcome back</h1>
            <p className="auth-description">Sign in to continue to your screening workspace.</p>
            {error && <div className="auth-error" role="alert">{error}</div>}
            <form className="auth-form" onSubmit={handleSubmit}>
                <div className="auth-field">
                    <label htmlFor="username">Username</label>
                    <input
                        type="text"
                        id="username"
                        name="username"
                        value={credentials.username}
                        onChange={handleChange}
                        autoComplete="username"
                        maxLength="128"
                        required
                    />
                </div>
                <div className="auth-field">
                    <label htmlFor="password">Password</label>
                    <input
                        type="password"
                        id="password"
                        name="password"
                        value={credentials.password}
                        onChange={handleChange}
                        autoComplete="current-password"
                        maxLength="128"
                        required
                    />
                </div>
                <button className="auth-submit" type="submit" disabled={isSubmitting}>
                    {isSubmitting ? 'Signing in...' : 'Sign in'}
                </button>
            </form>
            <p className="auth-switch">New to ResumeMetrics? <Link to="/signup">Create an account</Link></p>
        </AuthLayout>
    );
};

export default Login;
