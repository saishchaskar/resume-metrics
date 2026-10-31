// src/context/AuthContext.js
import React, { createContext, useState, useEffect } from 'react';
import api from '../api';

// Create the context
export const AuthContext = createContext();

// Create the provider component
export const AuthProvider = ({ children }) => {
    const [auth, setAuth] = useState({
        token: sessionStorage.getItem('token') || null,
        user: null,
    });

    useEffect(() => {
        localStorage.removeItem('token');
    }, []);

    useEffect(() => {
        const fetchUserData = async () => {
            if (auth.token) {
                try {
                    const response = await api.get('/user');
                    setAuth((prev) => ({ ...prev, user: response.data }));
                } catch (error) {
                    sessionStorage.removeItem('token');
                    setAuth({ token: null, user: null });
                }
            }
        };
        fetchUserData();
    }, [auth.token]);

    const login = async (credentials) => {
        try {
            const response = await api.post('/login', credentials);
            sessionStorage.setItem('token', response.data.token);
            setAuth({ token: response.data.token, user: response.data.user });
            return { success: true };
        } catch (error) {
            return { success: false, message: error.response?.data?.error || 'Login failed.' };
        }
    };

    const signup = async (userInfo) => {
        try {
            const response = await api.post('/signup', userInfo);
            sessionStorage.setItem('token', response.data.token);
            setAuth({ token: response.data.token, user: response.data.user });
            return { success: true };
        } catch (error) {
            return { success: false, message: error.response?.data?.error || 'Signup failed.' };
        }
    };

    const logout = () => {
        sessionStorage.removeItem('token');
        localStorage.removeItem('token');
        setAuth({ token: null, user: null });
    };

    return (
        <AuthContext.Provider value={{ auth, login, signup, logout }}>
            {children}
        </AuthContext.Provider>
    );
};
