"""Cloud Run Job entry point: validate and fan out one ZIP screening batch."""

import argparse
import hashlib
import json
import logging
import os
import tempfile
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath

import google.auth
from google.cloud import storage, tasks_v2
from google.cloud import firestore

from pipeline_store import batch_ref, candidate_collection, now_iso

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
logger = logging.getLogger('batch-preprocessor')

MAX_ZIP_BYTES = int(os.getenv('MAX_BATCH_ZIP_BYTES', str(1024 * 1024 * 1024)))
MAX_EXPANDED_BYTES = int(os.getenv('MAX_BATCH_EXPANDED_BYTES', str(10 * 1024 * 1024 * 1024)))
MAX_RESUME_BYTES = int(os.getenv('MAX_RESUME_BYTES', str(10 * 1024 * 1024)))
MAX_COMPRESSION_RATIO = 200
SUPPORTED_EXTENSIONS = {'.pdf', '.docx'}


def required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f'{name} must be configured for batch preprocessing.')
    return value


def enqueue_score(tasks_client, queue_path, worker_url, service_account, batch_id, candidate_id):
    task = {
        'name': f'{queue_path}/tasks/{batch_id}-{candidate_id}',
        'http_request': {
            'http_method': tasks_v2.HttpMethod.POST,
            'url': f"{worker_url.rstrip('/')}/api/internal/batch-score",
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({'batch_id': batch_id, 'candidate_id': candidate_id}).encode(),
            'oidc_token': {'service_account_email': service_account, 'audience': worker_url.rstrip('/')},
        }
    }
    tasks_client.create_task(request={'parent': queue_path, 'task': task})


def process(batch_id):
    project = os.getenv('GOOGLE_CLOUD_PROJECT') or os.getenv('GCP_PROJECT_ID') or google.auth.default()[1]
    bucket_name = required_env('GCS_BUCKET_NAME')
    location = required_env('CLOUD_TASKS_LOCATION')
    queue = required_env('CLOUD_TASKS_QUEUE')
    worker_url = required_env('BATCH_WORKER_URL')
    task_identity = required_env('CLOUD_TASKS_SERVICE_ACCOUNT')
    batch = batch_ref(batch_id).get()
    if not batch.exists:
        raise RuntimeError(f'Batch {batch_id} does not exist.')
    data = batch.to_dict()
    if data.get('state') == 'completed':
        logger.info('Batch %s already moved to state %s; skipping duplicate execution.', batch_id, data.get('state'))
        return
    batch_ref(batch_id).set({'state': 'preprocessing', 'updated_at': now_iso()}, merge=True)

    storage_client = storage.Client(project=project)
    blob = storage_client.bucket(bucket_name).blob(data['zip_object'])
    with tempfile.TemporaryDirectory(prefix='resumemetrics-batch-') as temp_dir:
        zip_path = os.path.join(temp_dir, 'batch.zip')
        blob.download_to_filename(zip_path)
        zip_size = os.path.getsize(zip_path)
        if zip_size > MAX_ZIP_BYTES:
            raise ValueError(f'ZIP exceeds the configured {MAX_ZIP_BYTES}-byte maximum.')

        uploads = []
        total_uncompressed = 0
        supported_count = 0
        with zipfile.ZipFile(zip_path) as archive:
            entries = archive.infolist()
            for info in entries:
                if info.is_dir():
                    continue
                extension = PurePosixPath(info.filename).suffix.lower()
                if extension not in SUPPORTED_EXTENSIONS:
                    continue
                if info.file_size > MAX_RESUME_BYTES:
                    raise ValueError(f'{PurePosixPath(info.filename).name} exceeds the per-resume limit.')
                compressed = max(info.compress_size, 1)
                if info.file_size / compressed > MAX_COMPRESSION_RATIO:
                    raise ValueError('ZIP contains a suspiciously compressed resume.')
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_EXPANDED_BYTES:
                    raise ValueError('ZIP expands beyond the configured total uncompressed byte limit.')
                supported_count += 1
            if not supported_count:
                raise ValueError('ZIP must contain at least one PDF or DOCX resume.')

            for archive_index, info in enumerate(entries):
                if info.is_dir() or PurePosixPath(info.filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                original = PurePosixPath(info.filename).name
                filename = ''.join(ch for ch in original if ch.isalnum() or ch in '._- ')[:180].strip() or 'resume'
                candidate_id = hashlib.sha256(f'{archive_index}:{info.filename}:{info.CRC}:{info.file_size}'.encode('utf-8')).hexdigest()[:32]
                object_name = f"batches/{data['owner_id']}/{batch_id}/resumes/{candidate_id}/{filename}"
                output = storage_client.bucket(bucket_name).blob(object_name)
                # The per-resume byte cap bounds this buffer to 10 MiB.
                output.upload_from_string(
                    archive.read(info),
                    content_type='application/pdf' if filename.lower().endswith('.pdf') else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                )
                candidate_ref = candidate_collection(batch_id).document(candidate_id)
                existing_candidate = candidate_ref.get()
                if not existing_candidate.exists:
                    candidate_ref.set({
                        'batch_id': batch_id, 'filename': filename, 'object_name': object_name,
                        'status': 'queued', 'score': 0.0, 'created_at': now_iso(),
                    })
                elif existing_candidate.to_dict().get('preprocess_enqueue_failed'):
                    candidate_ref.set({'status': 'queued', 'error': None, 'preprocess_enqueue_failed': False}, merge=True)
                uploads.append(candidate_id)

    prior_batch = batch_ref(batch_id).get().to_dict() or {}
    already_finished = int(prior_batch.get('completed_count', 0)) + int(prior_batch.get('failed_count', 0))
    batch_ref(batch_id).set({
        'state': 'completed' if uploads and already_finished >= len(uploads) else 'processing',
        'progress': {'current': min(already_finished, len(uploads)), 'total': len(uploads), 'filename': ''},
        'total_count': len(uploads),
        'updated_at': now_iso(),
    }, merge=True)

    tasks_client = tasks_v2.CloudTasksClient()
    parent = tasks_client.queue_path(project, location, queue)
    failures = []
    # Parallel enqueue keeps preprocessing time reasonable while Cloud Tasks
    # applies the queue's configured dispatch rate and concurrency limits.
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(enqueue_score, tasks_client, parent, worker_url, task_identity, batch_id, candidate_id) for candidate_id in uploads]
        for candidate_id, future in zip(uploads, futures):
            try:
                future.result()
            except Exception as exc:
                from google.api_core.exceptions import AlreadyExists
                if isinstance(exc, AlreadyExists):
                    continue
                logger.exception('Failed enqueue for batch %s candidate %s', batch_id, candidate_id)
                failures.append((candidate_id, exc))

    if failures:
        batch_ref(batch_id).set({'state': 'failed', 'error': f'{len(failures)} candidate scoring tasks could not be queued.', 'updated_at': now_iso()}, merge=True)
        for candidate_id, exc in failures:
            candidate_collection(batch_id).document(candidate_id).set({'status': 'failed', 'error': str(exc), 'score': 0.0, 'preprocess_enqueue_failed': True}, merge=True)
        raise RuntimeError(f'{len(failures)} candidate tasks failed to enqueue.')
    if not uploads:
        batch_ref(batch_id).set({'state': 'completed', 'updated_at': now_iso()}, merge=True)
    logger.info('Queued %s resumes for batch %s.', len(uploads), batch_id)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--batch-id', required=True)
    batch_id = parser.parse_args().batch_id
    try:
        process(batch_id)
    except Exception as exc:
        logger.exception('Preprocessing failed for batch %s.', batch_id)
        try:
            batch_ref(batch_id).set({
                'state': 'failed',
                'error': str(exc)[:500],
                'updated_at': now_iso(),
            }, merge=True)
        finally:
            raise

