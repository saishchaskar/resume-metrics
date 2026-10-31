import os
import zipfile
import tempfile
import logging
import uuid
import json
import secrets
from datetime import timedelta
from time import time
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
import pdfplumber
import docx
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import sqlite3
import threading
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
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_auth_login_attempts_ip_time ON auth_login_attempts (ip_address, attempted_at)')
    conn.commit()
    conn.close()

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
        return f'http://localhost:5000/api/local-test-resumes/{safe_name}'
    try:
        from google.cloud import storage
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(destination_blob_name)
        blob.upload_from_file(file_obj)
        # Generate a signed URL valid for 1 hour
        gcs_url = blob.generate_signed_url(expiration=3600, version='v4', method='GET')
        logging.debug(f"Uploaded {destination_blob_name} to GCS.")
        return gcs_url
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
        for page in pdf.pages:
            extracted_text = page.extract_text()
            if extracted_text:
                text += extracted_text + "\n"
    return text.strip()

# Function to process DOCX files
def extract_text_from_docx(file):
    doc = docx.Document(file)
    return '\n'.join([para.text for para in doc.paragraphs]).strip()

# Updated truncate_text function
def truncate_text(text, max_length=512):
    return text[:max_length]

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
        truncated_job_desc = truncate_text(job_description, max_length=512)
        job_embedding = model.encode(truncated_job_desc, convert_to_tensor=True)
        logging.debug(f"Task {task_id}: Computed embedding for job description.")

        for index, uploaded_file in enumerate(uploaded_files, start=1):
            filename = uploaded_file['filename']
            gcs_url = uploaded_file['gcs_url']
            file_path = os.path.join(tempfile.gettempdir(), f"downloaded_{filename}")
            try:
                # Download the file from GCS
                download_from_gcs(filename, file_path)
                logging.debug(f"Task {task_id}: Downloaded {filename} from GCS.")

                # Extract text
                if filename.lower().endswith('.pdf'):
                    extracted_text = extract_text_from_pdf(file_path)
                elif filename.lower().endswith('.docx'):
                    extracted_text = extract_text_from_docx(file_path)
                else:
                    logging.warning(f"Task {task_id}: Unsupported file format for {filename}. Skipping.")
                    continue

                # Truncate text
                truncated_resume_text = truncate_text(extracted_text, max_length=512)

                # Compute embedding for resume
                resume_embedding = model.encode(truncated_resume_text, convert_to_tensor=True)
                logging.debug(f"Task {task_id}: Computed embedding for {filename}.")

                # Compute cosine similarity
                similarity = util.pytorch_cos_sim(job_embedding, resume_embedding).item()

                # Add to ranked candidates
                ranked_candidates.append({'name': filename, 'score': similarity, 'url': gcs_url})
                logging.debug(f"Task {task_id}: Processed {filename} with similarity score {similarity}.")

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

        # Sort candidates by similarity score descending
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
    current_user_id = get_jwt_identity()
    logging.debug(f"User ID {current_user_id} initiated processing resumes.")

    if 'job_description' not in request.form or 'resumes_zip' not in request.files:
        logging.error("Missing job description or resumes zip")
        return jsonify({'error': 'Job description and resume zip are required.'}), 400

    job_description = request.form['job_description']
    resumes_zip = request.files['resumes_zip']
    logging.debug(f"Received resumes zip: {resumes_zip.filename}")

    with tempfile.TemporaryDirectory() as temp_dir:
        # Save the zip file
        zip_path = os.path.join(temp_dir, secure_filename(resumes_zip.filename))
        resumes_zip.save(zip_path)
        logging.debug(f"Saved zip file to {zip_path}")

        # Extract the zip file
        try:
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            logging.debug(f"Extracted zip file to {temp_dir}")
        except zipfile.BadZipFile:
            logging.error('Invalid ZIP file uploaded.')
            return jsonify({'error': 'Invalid ZIP file.'}), 400

        uploaded_files = []
        # Upload all extracted files to GCS
        for filename in os.listdir(temp_dir):
            file_path = os.path.join(temp_dir, filename)
            if filename.lower().endswith('.pdf') or filename.lower().endswith('.docx'):
                try:
                    with open(file_path, 'rb') as file_obj:
                        gcs_url = upload_to_gcs(file_obj, filename)
                        uploaded_files.append({'filename': filename, 'gcs_url': gcs_url})
                        logging.debug(f"Uploaded {filename} to GCS.")
                except Exception as e:
                    logging.error(f"Error uploading {filename} to GCS: {e}")
                    continue  # Skip file if upload fails
            else:
                logging.warning(f"Unsupported file format for {filename}. Skipping.")

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

# Route to fetch all results for the authenticated user
@app.route('/api/results', methods=['GET'])
@jwt_required()
def get_results():
    current_user_id = get_jwt_identity()
    logging.debug(f"User ID {current_user_id} requested their results.")

    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    results = cursor.execute('''
        SELECT id, timestamp, candidates FROM user_results
        WHERE user_id = ?
        ORDER BY timestamp DESC
    ''', (current_user_id,)).fetchall()
    conn.close()

    results_data = []
    for result in results:
        results_data.append({
            'id': result[0],
            'timestamp': result[1],
            'candidates': json.loads(result[2])
        })

    return jsonify({'results': results_data}), 200

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
    return send_from_directory(LOCAL_TEST_STORAGE, secure_filename(filename), as_attachment=True)

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

    import pandas as pd
    # Parse candidates
    candidates = json.loads(result[2])
   
    excel_path = f'results_{result_id}.xlsx'
    
    # Create a DataFrame
    import pandas as pd
    df = pd.DataFrame(candidates)
    df['Rank'] = df.index + 1  # Add a rank column
    df = df[['Rank', 'name', 'score', 'url']]  # Reorder columns

    # Save the DataFrame to an Excel file
    df.to_excel(excel_path, index=False)

    
    # Send the email with Excel attachment
    try:
        msg = Message(subject="Your Ranked Candidates Results",
                      recipients=[recipient_email])
        with app.open_resource(excel_path) as fp:
            msg.attach(excel_path, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', fp.read())
        
        mail.send(msg)
        logging.debug(f"Email sent to {recipient_email} for result ID {result_id}.")
        # Remove Excel file after sending to save space
        os.remove(excel_path)
        return jsonify({'message': 'Email sent successfully.'}), 200
    except Exception as e:
        logging.error(f"Error sending email to {recipient_email}: {e}")
        return jsonify({'error': 'Failed to send email.'}), 500

if __name__ == '__main__':
    # Cloud Run supplies PORT and requires the process to bind on all interfaces.
    # Keep Flask's usual port for local development when PORT is not set.
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')), debug=False, use_reloader=False)
