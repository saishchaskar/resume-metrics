import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';
import { AuthProvider } from './context/AuthContext';

jest.mock('axios', () => ({
  create: () => ({
    interceptors: { request: { use: jest.fn() } },
    get: jest.fn(),
    post: jest.fn(),
    delete: jest.fn(),
  }),
}));

const renderApp = () => render(<AuthProvider><App /></AuthProvider>);

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  window.history.replaceState({}, '', '/');
});

test('renders sign in and links to account creation', async () => {
  renderApp();
  expect(screen.getByRole('heading', { name: /welcome back/i })).toBeInTheDocument();
  await userEvent.click(screen.getByRole('link', { name: /create an account/i }));
  expect(screen.getByRole('heading', { name: /create your account/i })).toBeInTheDocument();
});

test('redirects an unauthenticated dashboard visit to the sign in page', () => {
  window.history.replaceState({}, '', '/dashboard');
  renderApp();
  expect(screen.getByRole('heading', { name: /welcome back/i })).toBeInTheDocument();
});
