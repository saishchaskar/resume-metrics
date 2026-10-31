# ResumeSignal

Resume screening workspace built with React and a Flask API.

## Screenshots

Desktop sign-in:

![ResumeSignal sign-in on desktop](docs/screenshots/login-desktop.png)

Mobile sign-in:

![ResumeSignal sign-in on mobile](docs/screenshots/login-mobile.png)

## Authentication and Deployment

- Passwords are sent only to the API over HTTPS in production and are hashed by Werkzeug before storage. Client-side password hashing is not a substitute for HTTPS.
- Set `APP_ENV=production` and provide a stable, private `JWT_SECRET_KEY` for the backend. The backend refuses to start in production without it. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- Set `CORS_ALLOWED_ORIGINS` to the exact HTTPS frontend origin when the API is hosted separately. The default allows only the local development origin.
- Set `REACT_APP_API_URL` to the HTTPS API origin when building the frontend for a separate API host. Same-origin development uses the React proxy.
- Production login and signup requests require HTTPS. When TLS terminates at a reverse proxy, ensure it sets `X-Forwarded-Proto` and that the Flask app is reachable only through that trusted proxy.
- Login failures are rate-limited to 10 attempts per IP per minute using the backend SQLite database. This limit is shared by processes using the same database file.
- Access tokens expire after 30 minutes and are stored in browser session storage. This limits persistence across browser sessions but does not protect against cross-site scripting; continue to prevent XSS and keep dependencies current.
- Do not commit service-account credentials. The local service-account JSON is excluded from Git and Docker build context. Because it was previously tracked, revoke and rotate that key in Google Cloud; ignoring the file does not remove copies from Git history.

Build the frontend with `npm run build`. Firebase Hosting serves the generated `build` directory. Run the Flask API behind an HTTPS-enabled production server and configure the environment values above before deployment.

