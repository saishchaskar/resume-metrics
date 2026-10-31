// src/components/Signup.js
import React, { useState, useContext } from 'react';
import { AuthContext } from '../context/AuthContext';
import { useNavigate, Link } from 'react-router-dom';
import AuthLayout from './AuthLayout';

const Signup = () => {
    const { signup } = useContext(AuthContext);
    const navigate = useNavigate();
    const [userInfo, setUserInfo] = useState({
        username: '',
        password: '',
    });
    const [error, setError] = useState('');
    const [isSubmitting, setIsSubmitting] = useState(false);

    const handleChange = (e) => {
        setUserInfo((prev) => ({ ...prev, [e.target.name]: e.target.value }));
    };

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError('');
        setIsSubmitting(true);
        try {
            const result = await signup(userInfo);
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
            <h1 className="auth-title">Create your account</h1>
            <p className="auth-description">Set up a private workspace for your screening team.</p>
            {error && <div className="auth-error" role="alert">{error}</div>}
            <form className="auth-form" onSubmit={handleSubmit}>
                <div className="auth-field">
                    <label htmlFor="username">Username</label>
                    <input
                        type="text"
                        id="username"
                        name="username"
                        value={userInfo.username}
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
                        value={userInfo.password}
                        onChange={handleChange}
                        autoComplete="new-password"
                        minLength="12"
                        maxLength="128"
                        required
                    />
                    <span className="auth-hint">Use at least 12 characters.</span>
                </div>
                <button className="auth-submit" type="submit" disabled={isSubmitting}>
                    {isSubmitting ? 'Creating account...' : 'Create account'}
                </button>
            </form>
            <p className="auth-switch">Already have an account? <Link to="/">Sign in</Link></p>
        </AuthLayout>
    );
};

export default Signup;
