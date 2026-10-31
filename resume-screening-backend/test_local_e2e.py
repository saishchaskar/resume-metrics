"""End-to-end API smoke test using local SQLite, storage, and ranking doubles."""

import importlib
import io
import os
import sys
import time
import zipfile

from docx import Document


def test_signup_upload_rank_results_and_delete(tmp_path, monkeypatch):
    backend_dir = os.path.dirname(__file__)
    monkeypatch.syspath_prepend(backend_dir)
    monkeypatch.setenv('RESUMESIGNAL_LOCAL_TEST', '1')
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'test-users.db'))
    monkeypatch.setenv('RESUMESIGNAL_TEST_STORAGE', str(tmp_path / 'storage'))
    monkeypatch.setenv('JWT_SECRET_KEY', 'local-test-signing-key-do-not-use-in-production')

    sys.modules.pop('app', None)
    app_module = importlib.import_module('app')
    client = app_module.app.test_client()

    signup = client.post('/api/signup', json={
        'username': 'integration-user',
        'password': 'test-password-long-enough',
    })
    assert signup.status_code == 201
    token = signup.get_json()['token']
    headers = {'Authorization': f'Bearer {token}'}

    login = client.post('/api/login', json={
        'username': 'integration-user',
        'password': 'test-password-long-enough',
    })
    assert login.status_code == 200

    resume = io.BytesIO()
    document = Document()
    document.add_paragraph('Python data engineering analytics candidate')
    document.save(resume)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr('candidate.docx', resume.getvalue())
    archive.seek(0)

    submitted = client.post(
        '/api/process',
        data={
            'job_description': 'Python data engineering analytics role',
            'resumes_zip': (archive, 'resumes.zip'),
        },
        headers=headers,
        content_type='multipart/form-data',
    )
    assert submitted.status_code == 202
    task_id = submitted.get_json()['task_id']

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        task = client.get(f'/api/result/{task_id}', headers=headers).get_json()
        if task['state'] != 'processing':
            break
        time.sleep(0.05)
    assert task['state'] == 'completed', task
    assert task['ranked_candidates'][0]['name'] == 'candidate.docx'
    assert task['ranked_candidates'][0]['score'] > 0
    assert client.get('/api/local-test-resumes/candidate.docx').status_code == 200

    saved = client.get('/api/results', headers=headers)
    assert saved.status_code == 200
    result = saved.get_json()['results'][0]
    assert result['candidates'][0]['name'] == 'candidate.docx'

    deleted = client.delete(f"/api/results/{result['id']}", headers=headers)
    assert deleted.status_code == 200
    assert client.get('/api/results', headers=headers).get_json()['results'] == []

    sys.modules.pop('app', None)
