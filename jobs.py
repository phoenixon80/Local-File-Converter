"""In-memory job store and temp-file lifecycle.

Single-user local tool, so a dict behind a lock is the right size for this.
Nothing here survives a restart by design; stale files on disk are swept at
startup and on an interval.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"

TEMP_DIR = Path(__file__).parent / "temp"


def _max_age() -> float:
    """How long finished work is kept, in seconds. TEMP_MAX_AGE_SECONDS=0
    sweeps everything, which is what the tests use."""
    raw = os.environ.get("TEMP_MAX_AGE_SECONDS")
    if raw is None:
        return 3600.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 3600.0


MAX_AGE_SECONDS = _max_age()  # sweep anything older than an hour by default


@dataclass
class Job:
    id: str
    filename: str
    source_ext: str
    target_format: str
    detected_type: str = ""
    status: str = QUEUED
    progress: int = 0
    stage: str = ""          # e.g. "png -> jpg (step 1 of 2)"
    error: str = ""          # short, human-readable
    details: str = ""        # full stderr, for the collapsible debug section
    input_path: Path | None = None
    output_path: Path | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def output_name(self) -> str:
        if self.output_path:
            return self.output_path.name
        return f"{Path(self.filename).stem}.{self.target_format}"

    def public(self) -> dict:
        return {
            "job_id": self.id,
            "filename": self.filename,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "detected_type": self.detected_type,
            "source_ext": self.source_ext,
            "target_format": self.target_format,
            "error": self.error,
            "details": self.details,
            "output_name": self.output_name if self.status == DONE else None,
            "download_url": f"/download/{self.id}" if self.status == DONE else None,
        }


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, filename: str, source_ext: str, target_format: str,
               detected_type: str, input_path: Path) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(id=job_id, filename=filename, source_ext=source_ext,
                  target_format=target_format, detected_type=detected_type,
                  input_path=input_path)
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **fields) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in fields.items():
                setattr(job, key, value)
            if job.status in (DONE, FAILED) and job.finished_at is None:
                job.finished_at = time.time()
            return job

    def all(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def sweep(self, max_age: float = MAX_AGE_SECONDS) -> int:
        """Drop finished jobs older than max_age, and their files."""
        cutoff = time.time() - max_age
        removed = 0
        with self._lock:
            stale = [j for j in self._jobs.values()
                     if j.created_at < cutoff and j.status in (DONE, FAILED)]
            for job in stale:
                del self._jobs[job.id]
        for job in stale:
            _remove_job_files(job)
            removed += 1
        return removed


def _remove_job_files(job: Job) -> None:
    for path in (job.input_path, job.output_path):
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
    work = TEMP_DIR / job.id
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)


def job_dir(job_id: str) -> Path:
    """Per-job working directory. Isolating jobs keeps concurrent runs from
    colliding over identically-named intermediate files."""
    path = TEMP_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def sweep_temp_dir(max_age: float = MAX_AGE_SECONDS) -> int:
    """Delete temp entries older than max_age, regardless of job state.

    Catches leftovers from a previous run, since the job store is memory-only
    and knows nothing about files written before a restart.
    """
    if not TEMP_DIR.exists():
        return 0
    cutoff = time.time() - max_age
    removed = 0
    for entry in TEMP_DIR.iterdir():
        if entry.name == ".gitkeep":
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed


store = JobStore()
