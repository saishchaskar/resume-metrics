import axios from 'axios';

const configuredApiUrl = process.env.REACT_APP_API_URL?.replace(/\/$/, '');
const defaultProductionApiUrl = 'https://flask-backend-app-879571205118.asia-south1.run.app';
export const apiOrigin = configuredApiUrl || (process.env.NODE_ENV === 'production' ? defaultProductionApiUrl : '');

if (
    process.env.NODE_ENV === 'production' &&
    apiOrigin &&
    !apiOrigin.startsWith('https://')
) {
    throw new Error('REACT_APP_API_URL must use HTTPS in production.');
}

const api = axios.create({
    baseURL: apiOrigin ? `${apiOrigin}/api` : '/api',
});

api.interceptors.request.use((config) => {
    const token = sessionStorage.getItem('token');
    if (token) {
        config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
});

export default api;
