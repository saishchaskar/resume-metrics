import axios from 'axios';

const configuredApiUrl = process.env.REACT_APP_API_URL?.replace(/\/$/, '');

if (
    process.env.NODE_ENV === 'production' &&
    configuredApiUrl &&
    !configuredApiUrl.startsWith('https://')
) {
    throw new Error('REACT_APP_API_URL must use HTTPS in production.');
}

const api = axios.create({
    baseURL: configuredApiUrl ? `${configuredApiUrl}/api` : '/api',
});

api.interceptors.request.use((config) => {
    const token = sessionStorage.getItem('token');
    if (token) {
        config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
});

export default api;