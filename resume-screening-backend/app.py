import os
import zipfile
import tempfile
import logging
import uuid
import json
import secrets
import re
import io
from datetime import timedelta
from time import time
from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename
import pdfplumber
import docx
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity, decode_token
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import sqlite3
import threading
from collections import defaultdict, deque
from hashlib import sha256
from flask_mail import Mail, Message
import smtplib
from flask import Flask, request, jsonify
from dotenv import load_dotenv  # Import dotenv

# Load environment variables from .env file
load_dotenv()

LOCAL_TEST_MODE = os.getenv('RESUMESIGNAL_LOCAL_TEST') == '1'
DATABASE_PATH = os.getenv('DATABASE_PATH', 'users.db')
LOCAL_TEST_STORAGE = os.getenv('RESUMESIGNAL_TEST_STORAGE', '/tmp/resumesignal-test-storage')

app = Flask(__name__)
MAX_REQUEST_BYTES = 30 * 1024 * 1024
MAX_ZIP_UPLOAD_BYTES = 29 * 1024 * 1024  # leave room for multipart headers
app.config['MAX_CONTENT_LENGTH'] = MAX_REQUEST_BYTES
MAX_RESUME_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_JOB_DESCRIPTION_CHARS = 20000
MAX_PDF_PAGES = 100
PIPELINE_ZIP_MAX_BYTES = int(os.getenv('MAX_BATCH_ZIP_BYTES', str(1024 * 1024 * 1024)))

# The frontend should use same-origin routing where possible. Configure explicit
# origins only when the frontend and API are deployed on separate hosts.
cors_origins = [origin.strip() for origin in os.getenv(
    'CORS_ALLOWED_ORIGINS', 'http://localhost:3000'
).split(',') if origin.strip()]
CORS(app, resources={r"/*": {"origins": cors_origins}}, allow_headers=["Content-Type", "Authorization"])

# Google Cloud Storage setup
BUCKET_NAME = os.getenv('GCS_BUCKET_NAME', 'resume-screening-resumes')

if LOCAL_TEST_MODE:
    # Deterministic lightweight embeddings for local integration testing.
    from collections import Counter
    from math import sqrt

    class LocalModel:
        def encode(self, text, convert_to_tensor=True):
            if isinstance(text, list):
                return [Counter(word.lower() for word in item.split()) for item in text]
            return Counter(word.lower() for word in text.split())

    class LocalSimilarity:
        @staticmethod
        def pytorch_cos_sim(left, right):
            dot = sum(left[word] * right[word] for word in set(left) & set(right))
            left_norm = sqrt(sum(value * value for value in left.values()))
            right_norm = sqrt(sum(value * value for value in right.values()))
            value = dot / (left_norm * right_norm) if left_norm and right_norm else 0.0
            return type('Score', (), {'item': lambda self: value})()

    model = LocalModel()
    util = LocalSimilarity()
else:
    from sentence_transformers import SentenceTransformer, util
    model = SentenceTransformer('all-MiniLM-L6-v2')

# Setup logging
logging.basicConfig(level=logging.DEBUG)

# In-memory task store
tasks = {}
request_windows = defaultdict(deque)
request_windows_lock = threading.Lock()
last_rate_window_cleanup = 0


def enforce_request_rate_limit(key, limit, window_seconds):
    """Best-effort per-instance request throttling; edge controls are still needed for DDoS."""
    global last_rate_window_cleanup
    now = time()
    with request_windows_lock:
        history = request_windows[key]
        while history and history[0] <= now - window_seconds:
            history.popleft()
        if len(history) >= limit:
            return max(1, int(window_seconds - (now - history[0])))
        history.append(now)

        # Prevent unbounded growth from requests carrying many distinct keys.
        if len(request_windows) > 5000 and now - last_rate_window_cleanup > 60:
            for stale_key, stale_history in list(request_windows.items()):
                while stale_history and stale_history[0] <= now - 3600:
                    stale_history.popleft()
                if not stale_history:
                    request_windows.pop(stale_key, None)
            last_rate_window_cleanup = now
    return None


@app.before_request
def api_rate_limit():
    if not request.path.startswith('/api/'):
        return None
    # Internal worker routes require a verified Cloud Tasks OIDC identity.
    # Their shared egress IP is governed by Cloud Tasks queue rate settings.
    if request.path.startswith('/api/internal/'):
        return None

    client_ip = request.remote_addr or 'unknown'
    path = request.path
    if path == '/api/login':
        key, limit, window = f'login:{client_ip}', 60, 60
    elif path == '/api/signup':
        key, limit, window = f'signup:{client_ip}', 5, 3600
    elif path == '/api/process':
        auth_header = request.headers.get('Authorization', '')
        token = auth_header.partition(' ')[2]
        try:
            identity = str(decode_token(token).get('sub')) if token else client_ip
        except Exception:
            identity = sha256(token.encode('utf-8')).hexdigest()[:24] if token else client_ip
        key, limit, window = f'process:{identity}', 10, 3600
    else:
        key, limit, window = f'api:{client_ip}', 300, 60

    retry_after = enforce_request_rate_limit(key, limit, window)
    if retry_after is not None:
        return jsonify({'error': 'Too many requests. Please wait before trying again.'}), 429, {'Retry-After': str(retry_after)}
    return None


@app.before_request
def block_unready_production_integrations():
    # Production integrations now persist through Firestore and Cloud Tasks.
    # Missing cloud configuration produces a specific error at enqueue time.
    return None


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({'error': 'Request is too large. ZIP uploads must be 29 MiB or smaller; individual resumes must be 10 MiB or smaller.'}), 413

# Configuration for JWT. Production must provide a stable, private secret.
app_environment = os.getenv('APP_ENV', 'development').lower()
jwt_secret = os.getenv('JWT_SECRET_KEY')
if not jwt_secret and app_environment == 'production':
    raise RuntimeError('JWT_SECRET_KEY must be configured in production.')
app.config['JWT_SECRET_KEY'] = jwt_secret or secrets.token_urlsafe(48)
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(minutes=30)

# Cloud hosting terminates TLS before forwarding requests to this app.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)

DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


def login_is_rate_limited(client_ip):
    if app_environment == 'production':
        from pipeline_store import firestore_client
        ip_key = sha256(client_ip.encode('utf-8')).hexdigest()
        snapshot = firestore_client().collection('login_attempt_windows').document(ip_key).get()
        attempts = snapshot.to_dict().get('attempts', []) if snapshot.exists else []
        return sum(float(value) > time() - 60 for value in attempts) >= 10
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        attempts = conn.execute(
            'SELECT COUNT(*) FROM auth_login_attempts WHERE ip_address = ? AND attempted_at > ?',
            (client_ip, time() - 60),
        ).fetchone()[0]
        return attempts >= 10
    finally:
        conn.close()


def record_login_failure(client_ip):
    if app_environment == 'production':
        from google.cloud import firestore
        from pipeline_store import firestore_client
        db = firestore_client()
        ref = db.collection('login_attempt_windows').document(sha256(client_ip.encode('utf-8')).hexdigest())
        transaction = db.transaction()
        @firestore.transactional
        def record(transaction):
            snapshot = ref.get(transaction=transaction)
            attempts = snapshot.to_dict().get('attempts', []) if snapshot.exists else []
            current_time = time()
            attempts = [float(value) for value in attempts if float(value) > current_time - 60]
            attempts.append(current_time)
            transaction.set(ref, {'attempts': attempts[-20:], 'updated_at': current_time}, merge=True)
        record(transaction)
        return
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        now = time()
        conn.execute('DELETE FROM auth_login_attempts WHERE attempted_at < ?', (now - 86400,))
        conn.execute(
            'INSERT INTO auth_login_attempts (ip_address, attempted_at) VALUES (?, ?)',
            (client_ip, now),
        )
        conn.commit()
    finally:
        conn.close()


def clear_login_failures(client_ip):
    if app_environment == 'production':
        from pipeline_store import firestore_client
        firestore_client().collection('login_attempt_windows').document(sha256(client_ip.encode('utf-8')).hexdigest()).delete()
        return
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    try:
        conn.execute('DELETE FROM auth_login_attempts WHERE ip_address = ?', (client_ip,))
        conn.commit()
    finally:
        conn.close()

jwt = JWTManager(app)

# Flask-Mail Configuration
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER', 'smtp.gmail.com')  # Example for Gmail
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True') == 'True'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')  # Your email address
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')  # Your email password or app-specific password
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD') 
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])

mail = Mail(app)  # Initialize Flask-Mail

# SQLite Database Initialization
def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    # Create users table if it doesn't exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    # Create user_results table if it doesn't exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            candidates TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS auth_login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT NOT NULL,
            attempted_at REAL NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS integration_jobs (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            external_ref TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    job_columns = {row[1] for row in cursor.execute('PRAGMA table_info(integration_jobs)').fetchall()}
    if 'status' not in job_columns:
        cursor.execute("ALTER TABLE integration_jobs ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_integration_jobs_user ON integration_jobs (user_id, created_at DESC)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS integration_api_keys (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            label TEXT NOT NULL,
            prefix TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            revoked_at DATETIME
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS integration_applications (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            candidate_name TEXT NOT NULL,
            candidate_email TEXT,
            filename TEXT NOT NULL,
            object_name TEXT NOT NULL,
            idempotency_key TEXT,
            status TEXT NOT NULL DEFAULT 'queued',
            score REAL,
            semantic_score REAL,
            analysis TEXT,
            error TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES integration_jobs(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_integration_apps_job_score ON integration_applications (job_id, status, score DESC)')
    application_columns = {row[1] for row in cursor.execute('PRAGMA table_info(integration_applications)').fetchall()}
    if 'idempotency_key' not in application_columns:
        cursor.execute('ALTER TABLE integration_applications ADD COLUMN idempotency_key TEXT')
    cursor.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_integration_apps_idempotency
        ON integration_applications (job_id, idempotency_key) WHERE idempotency_key IS NOT NULL''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_auth_login_attempts_ip_time ON auth_login_attempts (ip_address, attempted_at)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_results_user_timestamp ON user_results (user_id, timestamp DESC)')
    conn.commit()
    conn.close()


def create_integration_task(application_id):
    """Enqueue scoring with Cloud Tasks; local test mode uses a local worker thread."""
    if LOCAL_TEST_MODE:
        threading.Thread(target=process_integration_application, args=(application_id,), daemon=True).start()
        return

    project = os.getenv('GOOGLE_CLOUD_PROJECT') or os.getenv('GCP_PROJECT_ID')
    location = os.getenv('CLOUD_TASKS_LOCATION')
    queue = os.getenv('CLOUD_TASKS_QUEUE')
    worker_url = os.getenv('INTEGRATION_WORKER_URL') or os.getenv('BATCH_WORKER_URL')
    service_account = os.getenv('CLOUD_TASKS_SERVICE_ACCOUNT')
    if not all((project, location, queue, worker_url, service_account)):
        raise RuntimeError('Cloud Tasks integration is not configured.')

    from google.cloud import tasks_v2
    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(project, location, queue)
    task = {
        'http_request': {
            'http_method': tasks_v2.HttpMethod.POST,
            'url': f"{worker_url.rstrip('/')}/api/internal/integration-process",
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'application_id': application_id}).encode('utf-8'),
            'oidc_token': {
                'service_account_email': service_account,
                'audience': worker_url.rstrip('/'),
            },
        }
    }
    client.create_task(request={'parent': parent, 'task': task})


class RetryableScoringError(Exception):
    """A temporary worker error that Cloud Tasks should retry."""


def is_retryable_scoring_error(error):
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if error.__class__.__name__ in {'RetryError', 'TransportError'}:
        return True
    code = getattr(error, 'code', None)
    code = code() if callable(code) else code
    code = getattr(code, 'value', code)
    try:
        return int(code) in {408, 429, 500, 502, 503, 504}
    except (TypeError, ValueError):
        return False


def process_integration_application(application_id, retry_count=0):
    """Score one ATS submission and persist it so ranked results can update incrementally."""
    if app_environment == 'production':
        from pipeline_store import integration_application_ref, integration_job_ref, now_iso
        application_ref = integration_application_ref(application_id)
        application_snapshot = application_ref.get()
        if not application_snapshot.exists:
            return
        application = application_snapshot.to_dict()
        if application.get('status') == 'completed':
            return
        job_snapshot = integration_job_ref(application['job_id']).get()
        if not job_snapshot.exists:
            application_ref.set({'status': 'failed', 'error': 'The role record is unavailable.', 'updated_at': now_iso()}, merge=True)
            return
        application_ref.set({'status': 'processing', 'error': None, 'updated_at': now_iso()}, merge=True)
        file_path = os.path.join(tempfile.gettempdir(), f'integration_{uuid.uuid4().hex}')
        try:
            download_from_gcs(application['object_name'], file_path)
            if application['filename'].lower().endswith('.pdf'):
                extracted_text = extract_text_from_pdf(file_path)
            else:
                extracted_text = extract_text_from_docx(file_path)
            if not extracted_text:
                raise ValueError('No readable text was found in the resume.')
            description = job_snapshot.to_dict()['description']
            semantic = semantic_similarity(description, extracted_text)
            analysis = extract_resume_signals(extracted_text, meaningful_terms(description)[:40])
            application_ref.set({
                'status': 'completed', 'score': combined_match_score(semantic, analysis['keyword_coverage']),
                'semantic_score': semantic, 'analysis': analysis, 'error': None, 'updated_at': now_iso(),
            }, merge=True)
        except Exception as error:
            logging.exception('Integration application scoring failed for %s', application_id)
            if retry_count < 4 and is_retryable_scoring_error(error):
                application_ref.set({'status': 'queued', 'error': None, 'updated_at': now_iso()}, merge=True)
                raise RetryableScoringError('Temporary integration scoring failure.') from error
            application_ref.set({'status': 'failed', 'error': 'Could not process this resume. Check that it is a readable PDF or DOCX.', 'updated_at': now_iso()}, merge=True)
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
        return
    with sqlite3.connect(DATABASE_PATH, timeout=30) as conn:
        row = conn.execute('''
            SELECT a.job_id, a.user_id, a.filename, a.object_name, j.description
            FROM integration_applications a JOIN integration_jobs j ON j.id = a.job_id
            WHERE a.id = ?
        ''', (application_id,)).fetchone()
        if not row:
            return
        job_id, user_id, filename, object_name, job_description = row
        conn.execute("UPDATE integration_applications SET status = 'processing', error = NULL WHERE id = ?", (application_id,))
        conn.commit()

    file_path = os.path.join(tempfile.gettempdir(), f'integration_{uuid.uuid4().hex}')
    try:
        download_from_gcs(object_name, file_path)
        if filename.lower().endswith('.pdf'):
            extracted_text = extract_text_from_pdf(file_path)
        else:
            extracted_text = extract_text_from_docx(file_path)
        if not extracted_text:
            raise ValueError('No readable text was found in the resume.')
        semantic = semantic_similarity(job_description, extracted_text)
        analysis = extract_resume_signals(extracted_text, meaningful_terms(job_description)[:40])
        score = combined_match_score(semantic, analysis['keyword_coverage'])
        with sqlite3.connect(DATABASE_PATH, timeout=30) as conn:
            conn.execute('''
                UPDATE integration_applications
                SET status = 'completed', score = ?, semantic_score = ?, analysis = ?, error = NULL
                WHERE id = ?
            ''', (score, semantic, json.dumps(analysis), application_id))
            conn.commit()
    except Exception as exc:
        logging.exception('Integration application scoring failed for %s', application_id)
        with sqlite3.connect(DATABASE_PATH, timeout=30) as conn:
            conn.execute("UPDATE integration_applications SET status = 'failed', error = ? WHERE id = ?", ('Could not process this resume. Check that the file is a readable PDF or DOCX.', application_id))
            conn.commit()
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


def integration_owner_from_key():
    header = request.headers.get('Authorization', '')
    scheme, _, raw_key = header.partition(' ')
    if scheme.lower() != 'bearer' or not raw_key.startswith('rm_live_'):
        return None
    digest = sha256(raw_key.encode('utf-8')).hexdigest()
    if app_environment == 'production':
        from pipeline_store import firestore_client
        snapshot = firestore_client().collection('integration_api_keys').document(digest).get()
        if not snapshot.exists or snapshot.to_dict().get('revoked_at'):
            return None
        data = snapshot.to_dict()
        return data.get('id'), data.get('user_id')
    with sqlite3.connect(DATABASE_PATH) as conn:
        return conn.execute('''
            SELECT id, user_id FROM integration_api_keys
            WHERE key_hash = ? AND revoked_at IS NULL
        ''', (digest,)).fetchone()


def integration_api_error(message, status):
    return jsonify({'error': message}), status


@app.route('/api/integrations/jobs', methods=['GET', 'POST'])
@jwt_required()
def manage_integration_jobs():
    user_id = str(get_jwt_identity())
    if request.method == 'GET':
        if app_environment == 'production':
            from pipeline_store import firestore_client, count_query
            jobs = []
            query = firestore_client().collection('integration_jobs').where('user_id', '==', user_id).order_by('created_at', direction='DESCENDING')
            for snapshot in query.stream():
                item = dict(snapshot.to_dict(), id=snapshot.id)
                item['applicant_count'] = count_query(firestore_client().collection('integration_applications').where('job_id', '==', snapshot.id))
                jobs.append(item)
            return jsonify({'jobs': jobs})
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''SELECT j.id, j.title, j.description, j.external_ref, j.status, j.created_at,
                    (SELECT COUNT(*) FROM integration_applications a WHERE a.job_id = j.id) AS applicant_count
                FROM integration_jobs j WHERE j.user_id = ? ORDER BY j.created_at DESC''', (user_id,)).fetchall()
        return jsonify({'jobs': [dict(row) for row in rows]})

    data = request.get_json(silent=True) or {}
    title = data.get('title')
    description = data.get('description')
    external_ref = data.get('external_ref')
    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        return integration_api_error('Job title is required and must be at most 160 characters.', 400)
    if not isinstance(description, str) or not description.strip() or len(description) > MAX_JOB_DESCRIPTION_CHARS:
        return integration_api_error('Add a job description of up to 20,000 characters.', 400)
    if external_ref is not None and (not isinstance(external_ref, str) or len(external_ref) > 200):
        return integration_api_error('external_ref must be a string up to 200 characters.', 400)
    role_id = str(uuid.uuid4())
    if app_environment == 'production':
        from pipeline_store import integration_job_ref, now_iso
        integration_job_ref(role_id).set({
            'user_id': user_id, 'title': title.strip(), 'description': description.strip(),
            'external_ref': external_ref, 'status': 'open', 'applicant_count': 0,
            'created_at': now_iso(),
        })
        return jsonify({'id': role_id, 'title': title.strip(), 'external_ref': external_ref, 'status': 'open', 'applicant_count': 0}), 201
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute('''INSERT INTO integration_jobs (id, user_id, title, description, external_ref)
            VALUES (?, ?, ?, ?, ?)''', (role_id, user_id, title.strip(), description.strip(), external_ref))
        conn.commit()
    return jsonify({'id': role_id, 'title': title.strip(), 'external_ref': external_ref, 'status': 'open'}), 201


@app.route('/api/integrations/jobs/<job_id>', methods=['PATCH'])
@jwt_required()
def update_integration_job(job_id):
    user_id = str(get_jwt_identity())
    data = request.get_json(silent=True) or {}
    status = data.get('status')
    if status not in {'open', 'closed'}:
        return integration_api_error("status must be either 'open' or 'closed'.", 400)
    if app_environment == 'production':
        from pipeline_store import integration_job_ref, now_iso
        ref = integration_job_ref(job_id)
        snapshot = ref.get()
        if not snapshot.exists or snapshot.to_dict().get('user_id') != user_id:
            return integration_api_error('Job was not found.', 404)
        ref.set({'status': status, 'updated_at': now_iso()}, merge=True)
        return jsonify({'id': job_id, 'status': status})
    with sqlite3.connect(DATABASE_PATH) as conn:
        result = conn.execute('''UPDATE integration_jobs SET status = ?
            WHERE id = ? AND user_id = ?''', (status, job_id, user_id))
        conn.commit()
    if result.rowcount == 0:
        return integration_api_error('Job was not found.', 404)
    return jsonify({'id': job_id, 'status': status})


@app.route('/api/integrations/jobs/<job_id>/applications', methods=['GET'])
@jwt_required()
def get_integration_job_applications(job_id):
    user_id = str(get_jwt_identity())
    try:
        limit = max(1, min(100, int(request.args.get('limit', 50))))
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        return integration_api_error('limit and offset must be non-negative integers.', 400)
    if app_environment == 'production':
        from pipeline_store import firestore_client, integration_job_ref, count_query
        db = firestore_client()
        job_snapshot = integration_job_ref(job_id).get()
        if not job_snapshot.exists or job_snapshot.to_dict().get('user_id') != user_id:
            return integration_api_error('Job was not found.', 404)
        base = db.collection('integration_applications').where('job_id', '==', job_id).where('user_id', '==', user_id)
        rows = list(base.order_by('score', direction='DESCENDING').limit(limit).offset(offset).stream())
        total = count_query(base)
        counts = {status_name: count_query(base.where('status', '==', status_name)) for status_name in ('completed', 'queued', 'processing', 'failed')}
        return jsonify({
            'job': dict(job_snapshot.to_dict(), id=job_snapshot.id),
            'applications': [dict(row.to_dict(), id=row.id) for row in rows], 'total': total,
            'summary': {'total': total, **counts}, 'limit': limit, 'offset': offset,
        })
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        job = conn.execute('SELECT id, title, status FROM integration_jobs WHERE id = ? AND user_id = ?', (job_id, user_id)).fetchone()
        if not job:
            return integration_api_error('Job was not found.', 404)
        rows = conn.execute('''SELECT id, candidate_name, candidate_email, filename, object_name, status, score,
            semantic_score, analysis, error, created_at FROM integration_applications
            WHERE job_id = ? AND user_id = ? ORDER BY
            CASE WHEN score IS NULL THEN 1 ELSE 0 END, score DESC, created_at ASC
            LIMIT ? OFFSET ?''', (job_id, user_id, limit, offset)).fetchall()
        counts = {row[0]: row[1] for row in conn.execute('''SELECT status, COUNT(*) FROM integration_applications
            WHERE job_id = ? AND user_id = ? GROUP BY status''', (job_id, user_id)).fetchall()}
    applications = []
    for row in rows:
        item = dict(row)
        item['analysis'] = json.loads(item['analysis']) if item.get('analysis') else None
        applications.append(item)
    total = sum(counts.values())
    return jsonify({
        'job': dict(job),
        'applications': applications,
        'total': total,
        'summary': {
            'total': total,
            'completed': counts.get('completed', 0),
            'queued': counts.get('queued', 0),
            'processing': counts.get('processing', 0),
            'failed': counts.get('failed', 0),
        },
        'limit': limit,
        'offset': offset,
    })


@app.route('/api/integrations/keys', methods=['GET', 'POST'])
@jwt_required()
def manage_integration_keys():
    user_id = str(get_jwt_identity())
    if request.method == 'GET':
        if app_environment == 'production':
            from pipeline_store import firestore_client
            query = firestore_client().collection('integration_api_keys').where('user_id', '==', user_id).order_by('created_at', direction='DESCENDING')
            rows = []
            for snapshot in query.stream():
                item = snapshot.to_dict()
                item.pop('key_hash', None)
                rows.append(item)
            return jsonify({'keys': rows})
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''SELECT id, label, prefix, created_at, revoked_at
                FROM integration_api_keys WHERE user_id = ? ORDER BY created_at DESC''', (user_id,)).fetchall()
        return jsonify({'keys': [dict(row) for row in rows]})

    data = request.get_json(silent=True) or {}
    label = data.get('label', 'Careers portal')
    if not isinstance(label, str) or not label.strip() or len(label) > 80:
        return integration_api_error('Key label must be 1–80 characters.', 400)
    raw_key = 'rm_live_' + secrets.token_urlsafe(32)
    key_id = str(uuid.uuid4())
    prefix = raw_key[:16]
    if app_environment == 'production':
        from pipeline_store import firestore_client, now_iso
        digest = sha256(raw_key.encode('utf-8')).hexdigest()
        firestore_client().collection('integration_api_keys').document(digest).set({
            'id': key_id, 'user_id': user_id, 'label': label.strip(), 'prefix': prefix,
            'key_hash': digest, 'created_at': now_iso(), 'revoked_at': None,
        })
        return jsonify({'id': key_id, 'label': label.strip(), 'prefix': prefix, 'api_key': raw_key}), 201
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute('''INSERT INTO integration_api_keys (id, user_id, label, prefix, key_hash)
            VALUES (?, ?, ?, ?, ?)''', (key_id, user_id, label.strip(), prefix, sha256(raw_key.encode('utf-8')).hexdigest()))
        conn.commit()
    # The plaintext key is returned once and is never stored.
    return jsonify({'id': key_id, 'label': label.strip(), 'prefix': prefix, 'api_key': raw_key}), 201


@app.route('/api/integrations/keys/<key_id>', methods=['DELETE'])
@jwt_required()
def revoke_integration_key(key_id):
    user_id = str(get_jwt_identity())
    if app_environment == 'production':
        from pipeline_store import firestore_client, now_iso
        db = firestore_client()
        docs = db.collection('integration_api_keys').where('id', '==', key_id).where('user_id', '==', user_id).limit(1).stream()
        snapshot = next(docs, None)
        if not snapshot or snapshot.to_dict().get('revoked_at'):
            return integration_api_error('Active API key not found.', 404)
        snapshot.reference.set({'revoked_at': now_iso()}, merge=True)
        return jsonify({'revoked': True}), 200
    with sqlite3.connect(DATABASE_PATH) as conn:
        result = conn.execute('''UPDATE integration_api_keys SET revoked_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ? AND revoked_at IS NULL''', (key_id, user_id))
        conn.commit()
    if result.rowcount == 0:
        return integration_api_error('Active API key not found.', 404)
    return jsonify({'revoked': True}), 200


@app.route('/api/v1/jobs', methods=['GET'])
def integration_list_jobs():
    key = integration_owner_from_key()
    if not key:
        return integration_api_error('A valid ResumeMetrics API key is required.', 401)
    if app_environment == 'production':
        from pipeline_store import firestore_client
        query = firestore_client().collection('integration_jobs').where('user_id', '==', key[1]).order_by('created_at', direction='DESCENDING')
        return jsonify({'jobs': [dict(doc.to_dict(), id=doc.id) for doc in query.stream()]})
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('''SELECT id, title, external_ref, status, created_at FROM integration_jobs
            WHERE user_id = ? ORDER BY created_at DESC''', (key[1],)).fetchall()
    return jsonify({'jobs': [dict(row) for row in rows]})


@app.route('/api/v1/jobs/<job_id>/applications', methods=['POST', 'GET'])
def integration_applications(job_id):
    key = integration_owner_from_key()
    if not key:
        return integration_api_error('A valid ResumeMetrics API key is required.', 401)
    user_id = key[1]
    firestore_job = None
    if app_environment == 'production':
        from pipeline_store import integration_job_ref
        job_snapshot = integration_job_ref(job_id).get()
        if job_snapshot.exists and job_snapshot.to_dict().get('user_id') == user_id:
            firestore_job = job_snapshot.to_dict()
        if not firestore_job:
            return integration_api_error('Job was not found.', 404)
    with sqlite3.connect(DATABASE_PATH) as conn:
        job = None if app_environment == 'production' else conn.execute('SELECT id, title, description, status FROM integration_jobs WHERE id = ? AND user_id = ?', (job_id, user_id)).fetchone()
    if app_environment == 'production':
        job = (job_id, firestore_job.get('title'), firestore_job.get('description'), firestore_job.get('status'))
    if not job:
        return integration_api_error('Job was not found.', 404)

    if request.method == 'GET':
        try:
            limit = max(1, min(100, int(request.args.get('limit', 50))))
            offset = max(0, int(request.args.get('offset', 0)))
        except ValueError:
            return integration_api_error('limit and offset must be non-negative integers.', 400)
        if app_environment == 'production':
            from pipeline_store import firestore_client, count_query
            db = firestore_client()
            base = db.collection('integration_applications').where('job_id', '==', job_id).where('user_id', '==', user_id)
            rows = list(base.order_by('score', direction='DESCENDING').limit(limit).offset(offset).stream())
            total = count_query(base)
            return jsonify({'applications': [dict(row.to_dict(), id=row.id) for row in rows], 'total': total,
                            'limit': limit, 'offset': offset})
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''SELECT id, candidate_name, candidate_email, status, score,
                semantic_score, analysis, error, created_at FROM integration_applications
                WHERE job_id = ? AND user_id = ? ORDER BY
                CASE WHEN score IS NULL THEN 1 ELSE 0 END, score DESC, created_at ASC
                LIMIT ? OFFSET ?''', (job_id, user_id, limit, offset)).fetchall()
            total = conn.execute('SELECT COUNT(*) FROM integration_applications WHERE job_id = ? AND user_id = ?', (job_id, user_id)).fetchone()[0]
            counts = {row[0]: row[1] for row in conn.execute('''SELECT status, COUNT(*) FROM integration_applications
                WHERE job_id = ? AND user_id = ? GROUP BY status''', (job_id, user_id)).fetchall()}
        applications = []
        for row in rows:
            item = dict(row)
            item['analysis'] = json.loads(item['analysis']) if item.get('analysis') else None
            applications.append(item)
        return jsonify({
            'job': {'id': job[0], 'title': job[1], 'status': job[3]},
            'applications': applications,
            'total': total,
            'summary': {
                'total': total,
                'completed': counts.get('completed', 0),
                'queued': counts.get('queued', 0),
                'processing': counts.get('processing', 0),
                'failed': counts.get('failed', 0),
            },
            'limit': limit,
            'offset': offset,
        })

    if job[3] != 'open':
        return integration_api_error('This role is closed and no longer accepts applications.', 409)

    candidate_name = request.form.get('candidate_name', '').strip()
    candidate_email = request.form.get('candidate_email', '').strip()
    idempotency_key = request.headers.get('Idempotency-Key', '').strip() or None
    if idempotency_key and len(idempotency_key) > 128:
        return integration_api_error('Idempotency-Key must be at most 128 characters.', 400)
    if idempotency_key:
        if app_environment == 'production':
            from pipeline_store import firestore_client
            identity = sha256(f'{job_id}:{idempotency_key}'.encode('utf-8')).hexdigest()
            previous_snapshot = firestore_client().collection('integration_idempotency').document(identity).get()
            if previous_snapshot.exists:
                previous_id = previous_snapshot.to_dict().get('application_id')
                existing = firestore_client().collection('integration_applications').document(previous_id).get()
                if existing.exists:
                    previous_data = existing.to_dict()
                    if previous_data.get('status') in {'queued', 'processing', 'completed'}:
                        return jsonify({'id': previous_id, 'job_id': job_id, 'status': previous_data['status'], 'duplicate': True}), 200
                    try:
                        create_integration_task(previous_id)
                        existing.reference.set({'status': 'queued', 'error': None}, merge=True)
                        return jsonify({'id': previous_id, 'job_id': job_id, 'status': 'queued', 'duplicate': True}), 202
                    except Exception:
                        return integration_api_error('Application could not be queued. Retry with the same Idempotency-Key.', 503)
        with sqlite3.connect(DATABASE_PATH) as conn:
            previous = conn.execute('''SELECT id, status FROM integration_applications
                WHERE job_id = ? AND user_id = ? AND idempotency_key = ?''',
                (job_id, user_id, idempotency_key)).fetchone()
        if previous:
            if previous[1] in {'queued', 'processing', 'completed'}:
                return jsonify({'id': previous[0], 'job_id': job_id, 'status': previous[1], 'duplicate': True}), 200
            try:
                create_integration_task(previous[0])
                with sqlite3.connect(DATABASE_PATH) as conn:
                    conn.execute("UPDATE integration_applications SET status = 'queued', error = NULL WHERE id = ?", (previous[0],))
                    conn.commit()
                return jsonify({'id': previous[0], 'job_id': job_id, 'status': 'queued', 'duplicate': True}), 202
            except Exception:
                return integration_api_error('Application could not be queued. Check integration worker configuration and retry.', 503)
    resume = request.files.get('resume')
    if not candidate_name or len(candidate_name) > 160:
        return integration_api_error('candidate_name is required and must be at most 160 characters.', 400)
    if candidate_email and (len(candidate_email) > 254 or '@' not in candidate_email):
        return integration_api_error('candidate_email must be a valid email address.', 400)
    if not resume or not resume.filename:
        return integration_api_error('Upload a PDF or DOCX file in the resume field.', 400)
    filename = secure_filename(resume.filename)
    extension = os.path.splitext(filename)[1].lower()
    if extension not in {'.pdf', '.docx'}:
        return integration_api_error('Only PDF and DOCX resumes are accepted.', 415)
    resume.stream.seek(0, os.SEEK_END)
    file_size = resume.stream.tell()
    resume.stream.seek(0)
    if not file_size or file_size > MAX_RESUME_BYTES:
        return integration_api_error('Resume must be non-empty and no larger than 10 MiB.', 413)
    application_id = str(uuid.uuid4())
    object_name = f'integrations/{user_id}/{job_id}/{application_id}/{filename}'
    try:
        upload_to_gcs(resume.stream, object_name)
        if app_environment == 'production':
            from google.cloud import firestore
            from pipeline_store import firestore_client, integration_application_ref, now_iso
            db = firestore_client()
            application_ref = integration_application_ref(application_id)
            application_ref.set({
                'id': application_id, 'job_id': job_id, 'user_id': user_id,
                'candidate_name': candidate_name, 'candidate_email': candidate_email or None,
                'filename': filename, 'object_name': object_name, 'idempotency_key': idempotency_key,
                'status': 'queued', 'score': 0.0, 'created_at': now_iso(),
            })
            if idempotency_key:
                db.collection('integration_idempotency').document(sha256(f'{job_id}:{idempotency_key}'.encode('utf-8')).hexdigest()).set({'application_id': application_id})
            db.collection('integration_jobs').document(job_id).set({'applicant_count': firestore.Increment(1)}, merge=True)
            create_integration_task(application_id)
            return jsonify({'id': application_id, 'job_id': job_id, 'status': 'queued'}), 202
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.execute('''INSERT INTO integration_applications
                (id, job_id, user_id, candidate_name, candidate_email, filename, object_name, idempotency_key, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')''',
                (application_id, job_id, user_id, candidate_name, candidate_email or None, filename, object_name, idempotency_key))
            conn.commit()
        create_integration_task(application_id)
    except Exception:
        logging.exception('Could not accept integration application.')
        if app_environment == 'production':
            try:
                from pipeline_store import integration_application_ref, now_iso
                integration_application_ref(application_id).set({'status': 'failed', 'error': 'Application queue is temporarily unavailable.', 'updated_at': now_iso()}, merge=True)
            except Exception:
                logging.exception('Could not persist integration enqueue failure.')
        else:
            with sqlite3.connect(DATABASE_PATH) as conn:
                conn.execute("UPDATE integration_applications SET status = 'failed', error = ? WHERE id = ?",
                             ('Application queue is temporarily unavailable. Retry with the same Idempotency-Key.', application_id))
                conn.commit()
        return integration_api_error('Application could not be queued. Check integration worker configuration and retry.', 503)
    return jsonify({'id': application_id, 'job_id': job_id, 'status': 'queued'}), 202


@app.route('/api/internal/integration-process', methods=['POST'])
def integration_process_task():
    valid, error_response = verify_cloud_task_identity()
    if not valid:
        return error_response
    payload = request.get_json(silent=True) or {}
    application_id = payload.get('application_id')
    if not isinstance(application_id, str):
        return integration_api_error('application_id is required.', 400)
    try:
        retry_count = int(request.headers.get('X-CloudTasks-TaskRetryCount', '0'))
        process_integration_application(application_id, retry_count=retry_count)
    except RetryableScoringError:
        return jsonify({'error': 'Temporary scoring failure; retrying.'}), 503
    return jsonify({'processed': True}), 200


def verify_cloud_task_identity():
    """Validate Cloud Tasks OIDC for the private worker endpoints."""
    expected_email = os.getenv('CLOUD_TASKS_SERVICE_ACCOUNT')
    audience = (os.getenv('BATCH_WORKER_URL') or os.getenv('INTEGRATION_WORKER_URL', '')).rstrip('/')
    authorization = request.headers.get('Authorization', '')
    if not expected_email or not audience or not authorization.startswith('Bearer '):
        return False, (jsonify({'error': 'Task authentication is not configured.'}), 401)
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
        claims = id_token.verify_oauth2_token(authorization.split(' ', 1)[1], google_requests.Request(), audience=audience)
        if claims.get('email') != expected_email or not claims.get('email_verified'):
            return False, (jsonify({'error': 'Task identity is not authorized.'}), 403)
    except Exception:
        logging.warning('Cloud Tasks OIDC token verification failed.')
        return False, (jsonify({'error': 'Invalid task identity.'}), 401)
    return True, None


def process_batch_candidate(batch_id, candidate_id, retry_count=0):
    """Score one durable batch candidate, safe to retry from Cloud Tasks."""
    from pipeline_store import batch_ref, candidate_collection, firestore_client, now_iso
    batch = batch_ref(batch_id).get()
    candidate_ref = candidate_collection(batch_id).document(candidate_id)
    candidate_snapshot = candidate_ref.get()
    if not batch.exists or not candidate_snapshot.exists:
        return
    batch_data, candidate = batch.to_dict(), candidate_snapshot.to_dict()
    if candidate.get('status') in {'completed', 'failed'} and candidate.get('counter_applied'):
        return
    final_status = candidate.get('status') if candidate.get('status') in {'completed', 'failed'} else 'completed'
    if final_status not in {'completed', 'failed'}:
        candidate_ref.set({'status': 'processing', 'updated_at': now_iso()}, merge=True)
        file_path = os.path.join(tempfile.gettempdir(), f'batch_{uuid.uuid4().hex}')
        try:
            download_from_gcs(candidate['object_name'], file_path)
            if candidate['filename'].lower().endswith('.pdf'):
                extracted_text = extract_text_from_pdf(file_path)
            else:
                extracted_text = extract_text_from_docx(file_path)
            if not extracted_text:
                raise ValueError('No readable text was found in the resume.')
            semantic = semantic_similarity(batch_data['job_description'], extracted_text)
            analysis = extract_resume_signals(extracted_text, meaningful_terms(batch_data['job_description'])[:40])
            score = combined_match_score(semantic, analysis['keyword_coverage'])
            candidate_ref.set({
                'status': 'completed', 'score': score, 'semantic_score': semantic,
                'score_version': 2, 'analysis': analysis, 'updated_at': now_iso(),
            }, merge=True)
        except Exception as error:
            logging.exception('Batch candidate scoring failed: batch=%s candidate=%s', batch_id, candidate_id)
            if retry_count < 4 and is_retryable_scoring_error(error):
                candidate_ref.set({'status': 'queued', 'error': None, 'updated_at': now_iso()}, merge=True)
                raise RetryableScoringError('Temporary batch scoring failure.') from error
            final_status = 'failed'
            candidate_ref.set({'status': 'failed', 'score': 0.0, 'error': 'Could not process this resume. Check that it is a readable PDF or DOCX.', 'updated_at': now_iso()}, merge=True)
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

    firestore_batch = batch_ref(batch_id)
    firestore = firestore_client()
    @firestore.transactional
    def finish_candidate(transaction):
        current = firestore_batch.get(transaction=transaction)
        if not current.exists:
            return
        current_data = current.to_dict()
        candidate_state = candidate_ref.get(transaction=transaction)
        if not candidate_state.exists or candidate_state.to_dict().get('counter_applied'):
            return
        final_state = candidate_state.to_dict().get('status', final_status)
        done = int(current_data.get('completed_count', 0)) + int(final_state == 'completed')
        failed = int(current_data.get('failed_count', 0)) + int(final_state == 'failed')
        total = int(current_data.get('total_count', 0))
        state = 'completed' if total and done + failed >= total else 'processing'
        transaction.set(candidate_ref, {'counter_applied': True}, merge=True)
        transaction.set(firestore_batch, {
            'completed_count': done, 'failed_count': failed,
            'progress': {'current': done + failed, 'total': total, 'filename': candidate.get('filename', '')},
            'state': state, 'updated_at': now_iso(),
        }, merge=True)
    finish_candidate(firestore.transaction())


@app.route('/api/pipeline/batches', methods=['POST', 'GET'])
@jwt_required()
def pipeline_batches():
    from pipeline_store import firestore_client, create_batch, now_iso
    owner_id = str(get_jwt_identity())
    if request.method == 'GET':
        limit = max(1, min(100, int(request.args.get('limit', 50))))
        query = firestore_client().collection('screening_batches').where('owner_id', '==', owner_id).order_by('created_at', direction='DESCENDING').limit(limit)
        return jsonify({'batches': [dict(doc.to_dict(), id=doc.id) for doc in query.stream()]}), 200

    data = request.get_json(silent=True) or {}
    filename = secure_filename(str(data.get('filename') or 'resumes.zip'))
    description = data.get('job_description')
    try:
        byte_size = int(data.get('byte_size', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'byte_size must be an integer.'}), 400
    if not isinstance(description, str) or not description.strip() or len(description) > MAX_JOB_DESCRIPTION_CHARS:
        return jsonify({'error': 'A job description of up to 20,000 characters is required.'}), 400
    if not filename.lower().endswith('.zip') or byte_size < 1 or byte_size > PIPELINE_ZIP_MAX_BYTES:
        return jsonify({'error': f'Choose a ZIP file between 1 byte and {PIPELINE_ZIP_MAX_BYTES} bytes.'}), 413
    batch_id = create_batch(owner_id, description.strip(), f'batches/{owner_id}/{uuid.uuid4().hex}/upload.zip', filename, byte_size)
    try:
        from google.cloud import storage
        blob = storage.Client().bucket(BUCKET_NAME).blob(f'batches/{owner_id}/{batch_id}/upload.zip')
        session_url = blob.create_resumable_upload_session(
            content_type='application/zip', size=byte_size, origin=request.headers.get('Origin')
        )
        from pipeline_store import update_batch
        update_batch(batch_id, {'zip_object': f'batches/{owner_id}/{batch_id}/upload.zip', 'state': 'uploading'})
    except Exception:
        logging.exception('Could not create resumable ZIP upload session.')
        return jsonify({'error': 'Could not start the secure ZIP upload. Check Cloud Storage permissions and bucket CORS.'}), 503
    return jsonify({'batch_id': batch_id, 'upload_url': session_url, 'max_zip_bytes': PIPELINE_ZIP_MAX_BYTES}), 201


@app.route('/api/pipeline/batches/<batch_id>/start', methods=['POST'])
@jwt_required()
def start_pipeline_batch(batch_id):
    from pipeline_store import batch_ref, update_batch
    batch = batch_ref(batch_id).get()
    if not batch.exists or batch.to_dict().get('owner_id') != str(get_jwt_identity()):
        return jsonify({'error': 'Batch not found.'}), 404
    data = batch.to_dict()
    if data.get('state') not in {'uploading', 'uploaded', 'failed'}:
        return jsonify({'error': 'This batch has already been submitted.', 'state': data.get('state')}), 409
    try:
        from google.cloud import storage
        blob = storage.Client().bucket(BUCKET_NAME).blob(data['zip_object'])
        blob.reload()
        if not blob.size or blob.size > PIPELINE_ZIP_MAX_BYTES:
            return jsonify({'error': 'Uploaded ZIP is empty or exceeds the configured size limit.'}), 413
        if int(blob.size) != int(data['zip_bytes']):
            return jsonify({'error': 'Uploaded file size does not match the declared size.'}), 400
        from google.cloud import firestore
        from pipeline_store import firestore_client, now_iso
        db = firestore_client()
        transaction = db.transaction()
        @firestore.transactional
        def claim_batch(transaction):
            current = batch_ref(batch_id).get(transaction=transaction)
            if not current.exists or current.to_dict().get('owner_id') != str(get_jwt_identity()):
                return False
            if current.to_dict().get('state') not in {'uploading', 'uploaded', 'failed'}:
                return False
            transaction.set(batch_ref(batch_id), {'state': 'queued', 'updated_at': now_iso()}, merge=True)
            return True
        if not claim_batch(transaction):
            return jsonify({'error': 'This batch has already been submitted.'}), 409
        project = os.getenv('GOOGLE_CLOUD_PROJECT') or os.getenv('GCP_PROJECT_ID')
        region = os.getenv('CLOUD_RUN_JOB_REGION', os.getenv('CLOUD_TASKS_LOCATION', 'asia-south1'))
        job_name = os.getenv('BATCH_PREPROCESS_JOB')
        if not project or not job_name:
            raise RuntimeError('BATCH_PREPROCESS_JOB and GOOGLE_CLOUD_PROJECT must be configured.')
        from google.auth import default
        from google.auth.transport.requests import AuthorizedSession
        credentials, _ = default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        session = AuthorizedSession(credentials)
        if job_name.startswith('projects/'):
            resource_name = job_name
        else:
            resource_name = f'projects/{project}/locations/{region}/jobs/{job_name}'
        response = session.post(
            f'https://run.googleapis.com/v2/{resource_name}:run',
            json={'overrides': {'containerOverrides': [{'args': ['--batch-id', batch_id]}]}},
            timeout=30,
        )
        response.raise_for_status()
        operation = response.json().get('name')
        update_batch(batch_id, {'execution_operation': operation})
        return jsonify({'batch_id': batch_id, 'state': 'queued'}), 202
    except Exception:
        logging.exception('Could not start batch preprocessing job %s.', batch_id)
        try:
            update_batch(batch_id, {'state': 'uploaded', 'error': 'Could not launch the preprocessing job. Retry submission.'})
        except Exception:
            logging.exception('Could not release batch submission lock.')
        return jsonify({'error': 'Could not queue preprocessing. Check Cloud Run Job, Firestore, and task permissions.'}), 503


@app.route('/api/pipeline/batches/<batch_id>/candidates', methods=['GET'])
@jwt_required()
def pipeline_batch_candidates(batch_id):
    from pipeline_store import batch_ref, list_candidates
    batch = batch_ref(batch_id).get()
    if not batch.exists or batch.to_dict().get('owner_id') != str(get_jwt_identity()):
        return jsonify({'error': 'Batch not found.'}), 404
    try:
        limit = max(1, min(100, int(request.args.get('limit', 50))))
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        return jsonify({'error': 'limit and offset must be integers.'}), 400
    candidates = list_candidates(batch_id, limit, offset)
    total = int(batch.to_dict().get('total_count', 0))
    return jsonify({'batch': dict(batch.to_dict(), id=batch.id), 'candidates': candidates, 'total': total, 'limit': limit, 'offset': offset}), 200


@app.route('/api/internal/batch-score', methods=['POST'])
def batch_score_task():
    valid, error_response = verify_cloud_task_identity()
    if not valid:
        return error_response
    payload = request.get_json(silent=True) or {}
    batch_id, candidate_id = payload.get('batch_id'), payload.get('candidate_id')
    if not all(isinstance(value, str) and value for value in (batch_id, candidate_id)):
        return jsonify({'error': 'batch_id and candidate_id are required.'}), 400
    try:
        retry_count = int(request.headers.get('X-CloudTasks-TaskRetryCount', '0'))
        process_batch_candidate(batch_id, candidate_id, retry_count=retry_count)
    except RetryableScoringError:
        return jsonify({'error': 'Temporary scoring failure; retrying.'}), 503
    return jsonify({'processed': True}), 200

# Call init_db to ensure the database and tables are created
init_db()

# Function to upload files to GCP bucket
def upload_to_gcs(file_obj, destination_blob_name):
    if LOCAL_TEST_MODE:
        os.makedirs(LOCAL_TEST_STORAGE, exist_ok=True)
        safe_name = secure_filename(destination_blob_name)
        local_path = os.path.join(LOCAL_TEST_STORAGE, safe_name)
        file_obj.seek(0)
        with open(local_path, 'wb') as destination:
            destination.write(file_obj.read())
        return safe_name
    try:
        import google.auth
        from google.cloud import storage

        credentials, project_id = google.auth.default(
            scopes=['https://www.googleapis.com/auth/cloud-platform']
        )
        storage_client = storage.Client(project=project_id, credentials=credentials)
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(destination_blob_name)
        blob.upload_from_file(file_obj)
        logging.debug(f"Uploaded {destination_blob_name} to GCS.")
        return destination_blob_name
    except Exception as e:
        logging.error(f"Failed to upload {destination_blob_name} to GCS: {e}")
        raise

# Function to download files from GCS bucket
def download_from_gcs(blob_name, destination_path):
    if LOCAL_TEST_MODE:
        import shutil
        source_path = os.path.join(LOCAL_TEST_STORAGE, secure_filename(blob_name))
        shutil.copyfile(source_path, destination_path)
        return
    try:
        from google.cloud import storage
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_name)
        blob.download_to_filename(destination_path)
        logging.debug(f"Downloaded {blob_name} from GCS to {destination_path}")
    except Exception as e:
        logging.error(f"Failed to download {blob_name} from GCS: {e}")
        raise

# Function to process PDF files
def extract_text_from_pdf(file):
    text = ""
    with pdfplumber.open(file) as pdf:
        if len(pdf.pages) > MAX_PDF_PAGES:
            raise ValueError(f'Resume PDF exceeds the {MAX_PDF_PAGES}-page limit.')
        for page in pdf.pages:
            extracted_text = page.extract_text()
            if extracted_text:
                text += extracted_text + "\n"
    return text.strip()

# Function to process DOCX files
def extract_text_from_docx(file):
    doc = docx.Document(file)
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    blocks = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith('}p'):
            text = Paragraph(child, doc).text.strip()
            if text:
                blocks.append(text)
        elif child.tag.endswith('}tbl'):
            table = Table(child, doc)
            for row in table.rows:
                cells = [cell.text.strip().replace('\n', ' / ') for cell in row.cells]
                row_text = ' | '.join(cell for cell in cells if cell)
                if row_text:
                    blocks.append(row_text)
    return '\n'.join(blocks).strip()

# Updated truncate_text function
def truncate_text(text, max_length=512):
    return text[:max_length]


JOB_STOP_WORDS = {
    'and', 'the', 'for', 'with', 'from', 'that', 'this', 'will', 'you', 'your',
    'our', 'are', 'was', 'were', 'have', 'has', 'had', 'who', 'what', 'when',
    'where', 'how', 'all', 'any', 'can', 'may', 'must', 'not', 'into', 'over',
    'under', 'than', 'then', 'them', 'their', 'they', 'its', 'about', 'across',
    'role', 'work', 'team', 'years', 'year', 'experience', 'skills', 'skill',
    'strong', 'preferred', 'required', 'ability', 'responsibilities', 'including',
}


def meaningful_terms(text):
    terms = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{2,}", text.lower())
    return list(dict.fromkeys(term for term in terms if term not in JOB_STOP_WORDS))


def split_text_chunks(text, max_chars=1000):
    """Keep the whole resume in view of the embedding model, not just its first 512 chars."""
    text = re.sub(r'\s+', ' ', text or '').strip()
    if not text:
        return ['']
    return [text[start:start + max_chars] for start in range(0, len(text), max_chars)]


def semantic_similarity(job_text, resume_text):
    job_chunks = split_text_chunks(job_text)
    resume_chunks = split_text_chunks(resume_text)
    job_vectors = model.encode(job_chunks, convert_to_tensor=True)
    resume_vectors = model.encode(resume_chunks, convert_to_tensor=True)
    if LOCAL_TEST_MODE:
        scores = []
        for job_vector in job_vectors:
            best = 0.0
            for resume_vector in resume_vectors:
                shared = set(job_vector) & set(resume_vector)
                dot = sum(job_vector[token] * resume_vector[token] for token in shared)
                left = sum(value * value for value in job_vector.values()) ** 0.5
                right = sum(value * value for value in resume_vector.values()) ** 0.5
                best = max(best, dot / (left * right) if left and right else 0.0)
            scores.append(best)
        raw_score = sum(scores) / len(scores) if scores else 0.0
        return max(0.0, min(1.0, raw_score))
    matrix = util.pytorch_cos_sim(job_vectors, resume_vectors)
    raw_score = float(matrix.max(dim=1).values.mean().item())
    # Keep the displayed value in [0, 1]. This is a bounded similarity score,
    # not a probability or a calibrated prediction of job performance.
    return max(0.0, min(1.0, raw_score))


SEMANTIC_SCORE_WEIGHT = 0.75
KEYWORD_COVERAGE_WEIGHT = 0.25


def combined_match_score(semantic_score, keyword_coverage):
    """Return an explainable [0, 1] score from semantic and exact-term evidence."""
    semantic_score = max(0.0, min(1.0, float(semantic_score or 0.0)))
    keyword_coverage = max(0.0, min(1.0, float(keyword_coverage or 0.0)))
    return (SEMANTIC_SCORE_WEIGHT * semantic_score) + (KEYWORD_COVERAGE_WEIGHT * keyword_coverage)


def extract_resume_signals(text, job_terms):
    resume_terms = set(meaningful_terms(text))
    matched = [term for term in job_terms if term in resume_terms]
    missing = [term for term in job_terms if term not in resume_terms]
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]

    def evidence_for(pattern, limit=4):
        matcher = re.compile(pattern, re.IGNORECASE)
        return [line[:240] for line in lines if matcher.search(line)][:limit]

    return {
        'matched_terms': matched[:20],
        'missing_terms': missing[:20],
        'keyword_coverage': len(matched) / len(job_terms) if job_terms else 0.0,
        'achievement_evidence': evidence_for(r'\b(award|achievement|accomplish|recognition|impact|delivered|increased|improved|reduced|launched)\b'),
        'community_evidence': evidence_for(r'\b(volunteer|community|nonprofit|charity|civic|extracurricular|student club|social impact)\b'),
        'people_skill_evidence': evidence_for(r'\b(mentored|coached|collaborated|facilitated|negotiated|presented|led|leadership|communication|stakeholder)\b'),
        'employment_date_evidence': evidence_for(r'\b(19|20)\d{2}\b|\b(present|current)\b', limit=8),
    }

# Background task function
def process_resumes_background(job_description, uploaded_files, task_id, user_id):
    """
    Processes resumes: downloads from GCS, extracts text, computes similarity, and stores results.
    """
    try:
        logging.debug(f"Task {task_id}: Started processing resumes for user ID {user_id}.")
        ranked_candidates = []
        total_files = len(uploaded_files)

        # Compute embedding for job description
        job_terms = meaningful_terms(job_description)[:40]

        for index, uploaded_file in enumerate(uploaded_files, start=1):
            filename = uploaded_file['filename']
            object_name = uploaded_file['object_name']
            file_path = os.path.join(tempfile.gettempdir(), f"downloaded_{uuid.uuid4().hex}")
            try:
                # Download the file from GCS
                download_from_gcs(object_name, file_path)
                logging.debug(f"Task {task_id}: Downloaded {filename} from GCS.")

                # Extract text
                if filename.lower().endswith('.pdf'):
                    extracted_text = extract_text_from_pdf(file_path)
                elif filename.lower().endswith('.docx'):
                    extracted_text = extract_text_from_docx(file_path)
                else:
                    logging.warning(f"Task {task_id}: Unsupported file format for {filename}. Skipping.")
                    continue

                # Compare chunks from the full documents; combine the bounded
                # semantic and exact-term signals with explicit weights below.
                similarity = semantic_similarity(job_description, extracted_text)
                signals = extract_resume_signals(extracted_text, job_terms)
                match_score = combined_match_score(similarity, signals['keyword_coverage'])

                ranked_candidates.append({
                    'name': filename,
                    'score': match_score,
                    'semantic_score': similarity,
                    'score_version': 2,
                    'object_name': object_name,
                    'analysis': signals,
                })
                logging.debug(f"Task {task_id}: Processed {filename} with match score {match_score}.")

                # Update task progress
                tasks[task_id]['progress'] = {
                    'current': index,
                    'total': total_files,
                    'filename': filename
                }

                # Cleanup downloaded file
                os.remove(file_path)
                logging.debug(f"Task {task_id}: Cleaned up {filename}.")

            except Exception as e:
                logging.error(f"Task {task_id}: Error processing file {filename}: {e}")
                tasks[task_id]['status'] = 'failed'
                tasks[task_id]['error'] = f"Error processing file {filename}: {str(e)}"
                return  # Exit processing on error

        # Sort candidates by the explainable weighted match score descending.
        ranked_candidates.sort(key=lambda x: x['score'], reverse=True)
        logging.debug(f"Task {task_id}: Ranked candidates: {ranked_candidates}")

        # Store the result in the database
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO user_results (user_id, candidates) VALUES (?, ?)
        ''', (user_id, json.dumps(ranked_candidates)))
        conn.commit()
        result_id = cursor.lastrowid
        conn.close()
        logging.debug(f"Task {task_id}: Stored results in database with ID {result_id}.")

        # Update task status and results
        tasks[task_id]['status'] = 'completed'
        tasks[task_id]['result'] = ranked_candidates

    except Exception as e:
        logging.error(f"Task {task_id}: Unexpected error: {e}")
        tasks[task_id]['status'] = 'failed'
        tasks[task_id]['error'] = str(e)

@app.route('/api/signup', methods=['POST'])
def signup():
    if app_environment == 'production' and not request.is_secure:
        return jsonify({'error': 'HTTPS is required.'}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'A JSON request body is required.'}), 400
    username = data.get('username')
    password = data.get('password')

    if not isinstance(username, str) or not isinstance(password, str):
        return jsonify({'error': 'Username and password are required.'}), 400
    username = username.strip()
    if not username or len(username) > 128 or len(password) < 12 or len(password) > 128:
        return jsonify({'error': 'Use a username up to 128 characters and a password between 12 and 128 characters.'}), 400

    if app_environment == 'production':
        from google.cloud import firestore
        from pipeline_store import firestore_client, now_iso
        db = firestore_client()
        user_id = uuid.uuid4().hex
        user_ref = db.collection('users').document(user_id)
        handle_ref = db.collection('usernames').document(sha256(username.casefold().encode('utf-8')).hexdigest())
        transaction = db.transaction()
        @firestore.transactional
        def create_user(transaction):
            if handle_ref.get(transaction=transaction).exists:
                return False
            transaction.create(user_ref, {'username': username, 'password': generate_password_hash(password), 'created_at': now_iso()})
            transaction.create(handle_ref, {'user_id': user_id})
            return True
        try:
            created = create_user(transaction)
        except Exception:
            logging.exception('Could not create Firestore user account.')
            return jsonify({'error': 'Account creation is temporarily unavailable.'}), 503
        if not created:
            return jsonify({'error': 'User already exists'}), 409
        return jsonify({'token': create_access_token(identity=user_id), 'user': {'id': user_id, 'username': username}}), 201

    # Check if user already exists
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    existing_user = cursor.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    if existing_user:
        conn.close()
        return jsonify({'error': 'User already exists'}), 409

    # Create new user with hashed password
    hashed_password = generate_password_hash(password)  # Removed 'method' parameter
    try:
        cursor.execute('INSERT INTO users (username, password) VALUES (?, ?)', (username, hashed_password))
        conn.commit()
        user_id = cursor.lastrowid  # Get the inserted user's ID
    except sqlite3.Error as e:
        conn.close()
        logging.error(f"Database error during signup for user {username}: {e}")
        return jsonify({'error': 'Internal server error.'}), 500
    conn.close()

    # Create JWT token with user ID as identity
    access_token = create_access_token(identity=str(user_id))
    return jsonify({'token': access_token, 'user': {'id': user_id, 'username': username}}), 201

@app.route('/api/login', methods=['POST'])
def login():
    if app_environment == 'production' and not request.is_secure:
        return jsonify({'error': 'HTTPS is required.'}), 400

    client_ip = request.remote_addr or 'unknown'
    if login_is_rate_limited(client_ip):
        return jsonify({'error': 'Too many login attempts. Try again in one minute.'}), 429, {'Retry-After': '60'}

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'A JSON request body is required.'}), 400
    username = data.get('username')
    password = data.get('password')

    if not isinstance(username, str) or not isinstance(password, str):
        return jsonify({'error': 'Username and password are required.'}), 400
    username = username.strip()
    if not username or len(username) > 128 or not password or len(password) > 128:
        return jsonify({'error': 'Invalid credentials'}), 401

    if app_environment == 'production':
        from pipeline_store import firestore_client
        db = firestore_client()
        handle_id = sha256(username.casefold().encode('utf-8')).hexdigest()
        handle = db.collection('usernames').document(handle_id).get()
        user = None
        if handle.exists:
            user_id = handle.to_dict().get('user_id')
            user_snapshot = db.collection('users').document(str(user_id)).get()
            user = user_snapshot.to_dict() if user_snapshot.exists else None
        if not user or not check_password_hash(user.get('password', ''), password):
            if not user:
                check_password_hash(DUMMY_PASSWORD_HASH, password)
            record_login_failure(client_ip)
            return jsonify({'error': 'Invalid credentials'}), 401
        clear_login_failures(client_ip)
        token = create_access_token(identity=str(user_id))
        return jsonify({'token': token, 'user': {'id': str(user_id), 'username': user.get('username', username)}}), 200

    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    user = cursor.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()

    if not user:
        check_password_hash(DUMMY_PASSWORD_HASH, password)
        record_login_failure(client_ip)
        return jsonify({'error': 'Invalid credentials'}), 401

    if not check_password_hash(user[2], password):  # user[2] is the password field
        record_login_failure(client_ip)
        return jsonify({'error': 'Invalid credentials'}), 401

    # Create JWT token with user ID as identity
    clear_login_failures(client_ip)
    access_token = create_access_token(identity=str(user[0]))
    return jsonify({'token': access_token, 'user': {'id': user[0], 'username': username}}), 200

@app.route('/api/user', methods=['GET'])
@jwt_required()
def get_user():
    current_user_id = get_jwt_identity()
    logging.debug(f"Fetching data for user ID: {current_user_id}")

    if app_environment == 'production':
        from pipeline_store import firestore_client
        user = firestore_client().collection('users').document(str(current_user_id)).get()
        if not user.exists:
            return jsonify({'error': 'User not found.'}), 404
        return jsonify({'id': str(current_user_id), 'username': user.to_dict().get('username')}), 200
    
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    user = cursor.execute('SELECT id, username FROM users WHERE id = ?', (current_user_id,)).fetchone()
    conn.close()

    if not user:
        logging.error(f"User ID {current_user_id} not found.")
        return jsonify({'error': 'User not found.'}), 404

    return jsonify({'id': user[0], 'username': user[1]}), 200        

@app.route('/api/process', methods=['POST'])
@jwt_required()
def process_resumes_route():
    if app_environment == 'production':
        return jsonify({'error': 'Production ZIP screening uses direct resumable Cloud Storage uploads. Refresh the frontend and try again.'}), 409
    current_user_id = get_jwt_identity()
    logging.debug(f"User ID {current_user_id} initiated processing resumes.")

    if 'job_description' not in request.form or 'resumes_zip' not in request.files:
        logging.error("Missing job description or resumes zip")
        return jsonify({'error': 'Job description and resume zip are required.'}), 400

    job_description = request.form['job_description']
    resumes_zip = request.files['resumes_zip']
    if len(job_description) > MAX_JOB_DESCRIPTION_CHARS:
        return jsonify({'error': f'Job description must be {MAX_JOB_DESCRIPTION_CHARS} characters or fewer.'}), 413
    logging.debug(f"Received resumes zip: {resumes_zip.filename}")

    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, secure_filename(resumes_zip.filename or '') or 'resumes.zip')
        resumes_zip.save(zip_path)
        if os.path.getsize(zip_path) > MAX_ZIP_UPLOAD_BYTES:
            return jsonify({'error': 'ZIP files must be 29 MiB or smaller.'}), 413
        uploaded_files = []
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                entries = [info for info in zip_ref.infolist() if not info.is_dir()]
                supported = []
                total_uncompressed = 0
                for info in entries:
                    extension = os.path.splitext(info.filename)[1].lower()
                    if extension not in {'.pdf', '.docx'}:
                        continue
                    if info.file_size > MAX_RESUME_BYTES:
                        return jsonify({'error': f'{os.path.basename(info.filename)} exceeds the 10 MB per-resume limit.'}), 413
                    compressed_size = max(info.compress_size, 1)
                    if info.file_size / compressed_size > 200:
                        return jsonify({'error': 'ZIP archive contains a suspiciously compressed file.'}), 413
                    total_uncompressed += info.file_size
                    if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                        return jsonify({'error': 'ZIP archive expands beyond the 100 MB total limit.'}), 413
                    supported.append(info)

                for index, info in enumerate(supported, start=1):
                    extension = os.path.splitext(info.filename)[1].lower()
                    filename = secure_filename(os.path.basename(info.filename)) or f'resume-{index}{extension}'
                    file_path = os.path.join(temp_dir, f'{uuid.uuid4().hex}_{filename}')
                    written = 0
                    with zip_ref.open(info, 'r') as source, open(file_path, 'wb') as destination:
                        while True:
                            chunk = source.read(64 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > MAX_RESUME_BYTES:
                                raise ValueError(f'{filename} exceeds the 10 MB per-resume limit.')
                            destination.write(chunk)

                    object_name = f"{current_user_id}_{uuid.uuid4().hex}_{filename}"
                    with open(file_path, 'rb') as file_obj:
                        upload_to_gcs(file_obj, object_name)
                    uploaded_files.append({'filename': filename, 'object_name': object_name})
                    logging.debug(f"Uploaded {filename}.")
        except zipfile.BadZipFile:
            logging.error('Invalid ZIP file uploaded.')
            return jsonify({'error': 'Invalid ZIP file.'}), 400
        except (RuntimeError, ValueError) as e:
            logging.warning(f'ZIP upload rejected: {e}')
            return jsonify({'error': str(e)}), 413
        except Exception:
            logging.exception('Failed to process resume ZIP upload.')
            return jsonify({'error': 'Could not process this ZIP. Check that it contains valid PDF or DOCX resumes.'}), 400

        if not uploaded_files:
            logging.error("No valid resumes uploaded.")
            return jsonify({'error': 'No valid resumes uploaded.'}), 400

        # Generate a unique task ID
        task_id = str(uuid.uuid4())
        tasks[task_id] = {
            'user_id': current_user_id,  # Associate task with user
            'status': 'processing',
            'progress': {
                'current': 0,
                'total': len(uploaded_files),
                'filename': ''
            },
            'result': []
        }

        # Start background thread for processing
        thread = threading.Thread(target=process_resumes_background, args=(job_description, uploaded_files, task_id, current_user_id))
        thread.start()
        logging.debug(f"Enqueued background task with ID: {task_id} for user ID: {current_user_id}")

        return jsonify({'task_id': task_id}), 202

# Route to check task status and get results
@app.route('/api/result/<task_id>', methods=['GET'])
@jwt_required()
def get_result(task_id):
    current_user_id = get_jwt_identity()
    try:
        from pipeline_store import get_batch
        durable_batch = get_batch(task_id)
    except Exception:
        durable_batch = None
    if durable_batch:
        if durable_batch.get('owner_id') != str(current_user_id):
            return jsonify({'error': 'Unauthorized access to this batch.'}), 403
        return jsonify({
            'state': durable_batch.get('state', 'processing'),
            'progress': durable_batch.get('progress', {}),
            'error': durable_batch.get('error'),
            'result_count': durable_batch.get('completed_count', 0),
            'failed_count': durable_batch.get('failed_count', 0),
        }), 200
    if task_id not in tasks:
        logging.error(f"Task {task_id}: Invalid task ID.")
        return jsonify({'error': 'Invalid task ID.'}), 404

    task_info = tasks[task_id]

    # Check if the task belongs to the current user
    if task_info['user_id'] != current_user_id:
        logging.error(f"User ID {current_user_id} attempted to access task ID {task_id} belonging to user ID {task_info['user_id']}.")
        return jsonify({'error': 'Unauthorized access to this task.'}), 403

    response = {
        'state': task_info['status']
    }

    if task_info['status'] == 'processing':
        response['progress'] = task_info.get('progress', {})
    elif task_info['status'] == 'completed':
        response['ranked_candidates'] = task_info.get('result', [])
    elif task_info['status'] == 'failed':
        response['error'] = task_info.get('error', 'An error occurred during processing.')

    return jsonify(response)

# Route to fetch a page of saved results for the authenticated user.
@app.route('/api/results', methods=['GET'])
@jwt_required()
def get_results():
    current_user_id = get_jwt_identity()
    logging.debug(f"User ID {current_user_id} requested their results.")
    try:
        limit = max(1, min(100, int(request.args.get('limit', 50))))
        offset = max(0, int(request.args.get('offset', 0)))
    except (TypeError, ValueError):
        return jsonify({'error': 'limit and offset must be non-negative integers.'}), 400

    with sqlite3.connect(DATABASE_PATH) as conn:
        total = conn.execute(
            'SELECT COUNT(*) FROM user_results WHERE user_id = ?', (current_user_id,)
        ).fetchone()[0]
        results = conn.execute('''
        SELECT id, timestamp, candidates FROM user_results
        WHERE user_id = ?
        ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?
        ''', (current_user_id, limit, offset)).fetchall()

    results_data = []
    for result in results:
        results_data.append({
            'id': result[0],
            'timestamp': result[1],
            'candidates': json.loads(result[2])
        })

    return jsonify({'results': results_data, 'total': total, 'limit': limit, 'offset': offset}), 200

# Route to delete a specific result for the authenticated user
@app.route('/api/results/<int:result_id>', methods=['DELETE'])
@jwt_required()
def delete_result(result_id):
    current_user_id = get_jwt_identity()
    logging.debug(f"User ID {current_user_id} requested deletion of result ID {result_id}.")

    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    # Check if the result exists and belongs to the user
    result = cursor.execute('''
        SELECT * FROM user_results WHERE id = ? AND user_id = ?
    ''', (result_id, current_user_id)).fetchone()

    if not result:
        conn.close()
        logging.error(f"Result ID {result_id} not found for user ID {current_user_id}.")
        return jsonify({'error': 'Result not found.'}), 404

    # Delete the result
    cursor.execute('''
        DELETE FROM user_results WHERE id = ? AND user_id = ?
    ''', (result_id, current_user_id))
    conn.commit()
    conn.close()

    logging.debug(f"Result ID {result_id} deleted for user ID {current_user_id}.")
    return jsonify({'message': 'Result deleted successfully.'}), 200

# (Optional) Route to fetch a specific result for the authenticated user
@app.route('/api/results/<int:result_id>', methods=['GET'])
@jwt_required()
def get_specific_result(result_id):
    current_user_id = get_jwt_identity()
    logging.debug(f"User ID {current_user_id} requested retrieval of result ID {result_id}.")

    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    result = cursor.execute('''
        SELECT id, timestamp, candidates FROM user_results
        WHERE id = ? AND user_id = ?
    ''', (result_id, current_user_id)).fetchone()
    conn.close()

    if not result:
        logging.error(f"Result ID {result_id} not found for user ID {current_user_id}.")
        return jsonify({'error': 'Result not found.'}), 404

    result_data = {
        'id': result[0],
        'timestamp': result[1],
        'candidates': json.loads(result[2])
    }

    return jsonify({'result': result_data}), 200

@app.route('/api/local-test-resumes/<path:filename>', methods=['GET'])
def get_local_test_resume(filename):
    if not LOCAL_TEST_MODE:
        return jsonify({'error': 'Not found.'}), 404
    safe_name = secure_filename(filename)
    exact_path = os.path.join(LOCAL_TEST_STORAGE, safe_name)
    if not os.path.isfile(exact_path):
        matches = [name for name in os.listdir(LOCAL_TEST_STORAGE) if name.endswith(f'_{safe_name}')]
        if not matches:
            return jsonify({'error': 'Not found.'}), 404
        safe_name = matches[0]
    return send_from_directory(LOCAL_TEST_STORAGE, safe_name, as_attachment=True)


@app.route('/api/resume-preview/<path:object_name>', methods=['GET'])
@jwt_required()
def get_resume_preview(object_name):
    """Serve a resume only to the authenticated owner of its saved screening."""
    user_id = get_jwt_identity()
    conn = sqlite3.connect(DATABASE_PATH)
    saved_batches = conn.execute(
        'SELECT candidates FROM user_results WHERE user_id = ?', (user_id,)
    ).fetchall()
    conn.close()
    is_owned = any(
        candidate.get('object_name') == object_name or
        (not candidate.get('object_name') and candidate.get('name') == object_name)
        for (candidate_json,) in saved_batches
        for candidate in json.loads(candidate_json)
    )
    if not is_owned:
        with sqlite3.connect(DATABASE_PATH) as conn:
            is_owned = conn.execute('''SELECT 1 FROM integration_applications
                WHERE user_id = ? AND object_name = ? LIMIT 1''', (str(user_id), object_name)).fetchone() is not None
    if not is_owned:
        # Durable batch objects live below batches/{owner_id}/{batch_id}/...
        # Verify both owner and exact candidate record before reading private GCS data.
        parts = object_name.split('/')
        if len(parts) == 6 and parts[0] == 'batches' and parts[1] == str(user_id) and parts[3] == 'resumes':
            try:
                from pipeline_store import batch_ref, candidate_collection
                batch_id = parts[2]
                batch = batch_ref(batch_id).get()
                if batch.exists and batch.to_dict().get('owner_id') == str(user_id):
                    candidate = candidate_collection(batch_id).document(parts[4]).get()
                    is_owned = candidate.exists and candidate.to_dict().get('object_name') == object_name
            except Exception:
                logging.exception('Could not verify ownership of a pipeline resume.')
        elif app_environment == 'production' and len(parts) >= 5 and parts[0] == 'integrations' and parts[1] == str(user_id):
            try:
                from pipeline_store import integration_application_ref
                application = integration_application_ref(parts[3]).get()
                is_owned = application.exists and application.to_dict().get('user_id') == str(user_id) and application.to_dict().get('object_name') == object_name
            except Exception:
                logging.exception('Could not verify ownership of an integration resume.')
    if not is_owned:
        return jsonify({'error': 'Resume not found.'}), 404

    extension = os.path.splitext(object_name)[1].lower()
    if extension not in {'.pdf', '.docx'}:
        return jsonify({'error': 'Unsupported resume format.'}), 415

    try:
        if LOCAL_TEST_MODE:
            local_path = os.path.join(LOCAL_TEST_STORAGE, secure_filename(object_name))
            with open(local_path, 'rb') as resume_file:
                resume_bytes = resume_file.read()
        else:
            from google.cloud import storage
            resume_bytes = storage.Client().bucket(BUCKET_NAME).blob(object_name).download_as_bytes()
    except FileNotFoundError:
        return jsonify({'error': 'Resume file is unavailable.'}), 404
    except Exception:
        logging.exception('Could not read resume object for preview.')
        return jsonify({'error': 'Could not load this resume.'}), 502

    disposition = request.args.get('download') == '1'
    if request.args.get('format') == 'text' and extension == '.docx' and not disposition:
        return jsonify({'text': extract_text_from_docx(io.BytesIO(resume_bytes))}), 200, {'Cache-Control': 'private, no-store'}

    media_type = 'application/pdf' if extension == '.pdf' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    response = send_file(
        io.BytesIO(resume_bytes),
        mimetype=media_type,
        as_attachment=disposition,
        download_name=secure_filename(request.args.get('filename') or os.path.basename(object_name)) or f'resume{extension}',
        conditional=False,
    )
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

def results_xlsx(candidates):
    """Build an Excel workbook in memory so exports work on read-only containers."""
    import pandas as pd

    rows = []
    for rank, candidate in enumerate(candidates, start=1):
        analysis = candidate.get('analysis') or {}
        rows.append({
            'Rank': rank,
            'Candidate': candidate.get('name', ''),
            'Overall match score (0-1)': candidate.get('score', 0),
            'Semantic similarity (0-1)': candidate.get('semantic_score', candidate.get('score', 0)),
            'Exact keyword coverage': analysis.get('keyword_coverage', 0),
            'Matched terms': ', '.join(analysis.get('matched_terms', [])),
            'Missing terms': ', '.join(analysis.get('missing_terms', [])),
            'Achievement evidence': ' | '.join(analysis.get('achievement_evidence', [])),
            'Community evidence': ' | '.join(analysis.get('community_evidence', [])),
            'People-skill evidence': ' | '.join(analysis.get('people_skill_evidence', [])),
            'Employment date evidence': ' | '.join(analysis.get('employment_date_evidence', [])),
        })
    output = io.BytesIO()
    pd.DataFrame(rows).to_excel(output, index=False, engine='openpyxl')
    output.seek(0)
    return output


@app.route('/api/results/<int:result_id>/export.xlsx', methods=['GET'])
@jwt_required()
def export_results_xlsx(result_id):
    user_id = get_jwt_identity()
    with sqlite3.connect(DATABASE_PATH) as conn:
        result = conn.execute(
            'SELECT timestamp, candidates FROM user_results WHERE id = ? AND user_id = ?',
            (result_id, user_id),
        ).fetchone()
    if not result:
        return jsonify({'error': 'Result not found.'}), 404

    filename = f'resumemetrics-results-{result_id}.xlsx'
    response = send_file(
        results_xlsx(json.loads(result[1])),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename,
    )
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@app.route('/api/send-email', methods=['POST'])
@jwt_required()
def send_email():
    """
    Endpoint to send ranked candidates results via email.
    Expects JSON data with 'email' and 'result_id'.
    """
    data = request.get_json()

    if not data:
        logging.error("No data provided in send-email request.")
        return jsonify({'error': 'No data provided.'}), 400

    recipient_email = data.get('email')
    result_id = data.get('result_id')

    if not recipient_email or not result_id:
        logging.error("Missing email or result_id in send-email request.")
        return jsonify({'error': 'Email and result_id are required.'}), 400

    # Validate email format (basic validation)
    if not isinstance(recipient_email, str) or '@' not in recipient_email:
        logging.error(f"Invalid email format: {recipient_email}")
        return jsonify({'error': 'Invalid email format.'}), 400

    # Fetch the specific result
    current_user_id = get_jwt_identity()
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    result = cursor.execute(''' 
        SELECT id, timestamp, candidates FROM user_results 
        WHERE id = ? AND user_id = ? 
    ''', (result_id, current_user_id)).fetchone()
    conn.close()

    if not result:
        logging.error(f"Result ID {result_id} not found for user ID {current_user_id}.")
        return jsonify({'error': 'Result not found.'}), 404

    candidates = json.loads(result[2])
    # Send the email with Excel attachment
    try:
        msg = Message(subject="Your Ranked Candidates Results",
                      recipients=[recipient_email])
        msg.attach(
            f'resumemetrics-results-{result_id}.xlsx',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            results_xlsx(candidates).read(),
        )
        mail.send(msg)
        logging.debug(f"Email sent to {recipient_email} for result ID {result_id}.")
        return jsonify({'message': 'Email sent successfully.'}), 200
    except Exception as e:
        logging.error(f"Error sending email to {recipient_email}: {e}")
        return jsonify({'error': 'Failed to send email.'}), 500

if __name__ == '__main__':
    # Cloud Run supplies PORT and requires the process to bind on all interfaces.
    # Keep Flask's usual port for local development when PORT is not set.
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')), debug=False, use_reloader=False)
