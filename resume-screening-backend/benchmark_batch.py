"""Benchmark local PDF/DOCX extraction and ranking using the production model.

Usage:
  .venv/bin/python resume-screening-backend/benchmark_batch.py /path/to/batch.zip \
      --job-description-file /path/to/job-description.txt

This does not measure Cloud Run, GCS network transfer, or concurrent load. Use a
representative private batch and compare results only on the same machine/model.
"""

import argparse
import io
import json
import os
import platform
import resource
import statistics
import sys
import tempfile
import time
import zipfile
from pathlib import Path

MAX_ZIP_BYTES = 29 * 1024 * 1024
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_RESUME_BYTES = 10 * 1024 * 1024


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('zip_file', type=Path)
    parser.add_argument('--job-description-file', type=Path, required=True)
    args = parser.parse_args()

    if os.getenv('RESUMESIGNAL_LOCAL_TEST') == '1':
        parser.error('Unset RESUMESIGNAL_LOCAL_TEST; benchmark with the production embedding model.')
    if not args.zip_file.is_file() or not args.job_description_file.is_file():
        parser.error('Provide an existing ZIP file and job-description text file.')
    zip_bytes = args.zip_file.stat().st_size
    if zip_bytes > MAX_ZIP_BYTES:
        parser.error('ZIP exceeds the current 29 MiB application upload cap.')

    with zipfile.ZipFile(args.zip_file) as archive:
        resumes = [entry for entry in archive.infolist()
                   if not entry.is_dir() and Path(entry.filename).suffix.lower() in {'.pdf', '.docx'}]
        if not resumes:
            parser.error('ZIP must contain at least one PDF or DOCX resume.')
        expanded_bytes = sum(entry.file_size for entry in resumes)
        if expanded_bytes > MAX_EXPANDED_BYTES:
            parser.error('Supported resumes expand beyond the current 100 MiB cap.')
        if any(entry.file_size > MAX_RESUME_BYTES for entry in resumes):
            parser.error('A resume exceeds the current 10 MiB per-file cap.')

        os.environ.setdefault('APP_ENV', 'development')
        os.environ.setdefault('JWT_SECRET_KEY', 'benchmark-only-secret-never-use-in-production')
        os.environ['DATABASE_PATH'] = os.path.join(tempfile.gettempdir(), f'resumemetrics-benchmark-{os.getpid()}.db')
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        startup_started = time.perf_counter()
        try:
            from app import extract_text_from_docx, extract_text_from_pdf, semantic_similarity
        except ImportError as error:
            parser.error(
                f'Production model dependencies are missing ({error}). '
                'Use Python 3.10 and install resume-screening-backend/requirements.txt.'
            )
        model_startup_seconds = time.perf_counter() - startup_started

        job_description = args.job_description_file.read_text(encoding='utf-8')
        resume_seconds = []
        scores = []
        processing_started = time.perf_counter()
        for entry in resumes:
            started = time.perf_counter()
            content = archive.read(entry)
            if entry.filename.lower().endswith('.pdf'):
                text = extract_text_from_pdf(io.BytesIO(content))
            else:
                text = extract_text_from_docx(io.BytesIO(content))
            scores.append(semantic_similarity(job_description, text))
            resume_seconds.append(time.perf_counter() - started)
        processing_seconds = time.perf_counter() - processing_started

    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != 'darwin':
        peak_rss *= 1024  # Linux reports KiB; macOS reports bytes.
    print(json.dumps({
        'machine': platform.machine(),
        'python': sys.version.split()[0],
        'model': 'all-MiniLM-L6-v2',
        'resumes': len(resumes),
        'zip_bytes': zip_bytes,
        'expanded_resume_bytes': expanded_bytes,
        'model_startup_seconds': round(model_startup_seconds, 3),
        'processing_seconds_excluding_model_startup': round(processing_seconds, 3),
        'resumes_per_minute_local': round(len(resumes) * 60 / processing_seconds, 2) if processing_seconds else None,
        'per_resume_seconds_median': round(statistics.median(resume_seconds), 3),
        'per_resume_seconds_p95': round(percentile(resume_seconds, 0.95), 3),
        'peak_rss_bytes_including_model': peak_rss,
        'score_min': round(min(scores), 4),
        'score_max': round(max(scores), 4),
        'scope': 'Local sequential extraction and inference only; excludes Cloud Run startup, GCS, upload, and concurrent users.',
    }, indent=2))


if __name__ == '__main__':
    main()
