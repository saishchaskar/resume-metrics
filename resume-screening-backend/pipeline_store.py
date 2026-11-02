"""Durable Firestore and Cloud Tasks helpers for ZIP screening batches."""

import os
from datetime import datetime, timezone


def firestore_client():
    from google.cloud import firestore

    project = os.getenv('GOOGLE_CLOUD_PROJECT') or os.getenv('GCP_PROJECT_ID')
    return firestore.Client(project=project or None)


def batch_ref(batch_id):
    return firestore_client().collection('screening_batches').document(batch_id)


def candidate_collection(batch_id):
    return batch_ref(batch_id).collection('candidates')


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def create_batch(owner_id, description, object_name, filename, byte_size):
    ref = batch_ref(__import__('uuid').uuid4().hex)
    ref.set({
        'owner_id': str(owner_id),
        'job_description': description,
        'zip_object': object_name,
        'zip_filename': filename,
        'zip_bytes': byte_size,
        'state': 'uploaded',
        'progress': {'current': 0, 'total': 0, 'filename': ''},
        'completed_count': 0,
        'failed_count': 0,
        'created_at': now_iso(),
        'updated_at': now_iso(),
    })
    return ref.id


def update_batch(batch_id, values):
    values['updated_at'] = now_iso()
    batch_ref(batch_id).set(values, merge=True)


def get_batch(batch_id):
    snapshot = batch_ref(batch_id).get()
    return snapshot.to_dict() if snapshot.exists else None


def upsert_candidate(batch_id, candidate_id, values):
    candidate_collection(batch_id).document(candidate_id).set(values, merge=True)


def list_candidates(batch_id, limit=50, offset=0):
    query = candidate_collection(batch_id).order_by('score', direction='DESCENDING')
    if offset:
        query = query.offset(offset)
    documents = list(query.limit(limit).stream())
    return [dict(doc.to_dict(), id=doc.id) for doc in documents]


def integration_job_ref(job_id):
    return firestore_client().collection('integration_jobs').document(job_id)


def integration_application_ref(application_id):
    return firestore_client().collection('integration_applications').document(application_id)


def count_query(query):
    result = query.count().get()
    return result[0][0].value if result and result[0] else 0

