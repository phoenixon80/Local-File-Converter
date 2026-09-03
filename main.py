"""Local file converter: a router in front of FFmpeg, ImageMagick, Pandoc,
LibreOffice and Calibre. Nothing leaves the machine."""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import binaries
import jobs
import registry
from detect import detect
from handlers import base as handler_base
from handlers.base import ConversionError
from jobs import DONE, FAILED, RUNNING, store

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
SWEEP_INTERVAL_SECONDS = 600

STATIC_DIR = Path(__file__).parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    jobs.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    # Files can outlive the process; the job store cannot. Sweep on the way up.
    swept = jobs.sweep_temp_dir()
    if swept:
        print(f"[startup] removed {swept} stale temp item(s)")
    missing = [t["display"] for t in binaries.health().values() if not t["present"]]
    if missing:
        print(f"[startup] tools not found: {', '.join(missing)} "
              f"(conversions needing them will be rejected with an explanation)")

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            store.sweep()
            jobs.sweep_temp_dir()

    task = asyncio.create_task(sweeper())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # Take any running conversions down with the server. A force-kill of
        # the process cannot be caught, but Ctrl+C and SIGTERM come through
        # here, and those are how this app actually gets stopped.
        stopped = handler_base.terminate_all()
        if stopped:
            print(f"[shutdown] stopped {stopped} running conversion(s)")


app = FastAPI(title="Local File Converter", lifespan=lifespan)


# ------------------------------------------------------------------ the work

def run_conversion(job_id: str) -> None:
    """Execute a job's conversion. Runs in a background thread.

    A plan is one route, or two when the pair needs an intermediate format.
    """
    job = store.get(job_id)
    if job is None:
        return
    store.update(job_id, status=RUNNING, progress=5,
                 stage=f"{job.source_ext} -> {job.target_format}")
    try:
        steps = registry.plan(job.source_ext, job.target_format)
        work = jobs.job_dir(job_id)
        stem = Path(job.filename).stem
        current = Path(job.input_path)

        for index, route in enumerate(steps):
            last = index == len(steps) - 1
            # Only the final artefact gets the user-facing name; intermediates
            # are named so a chain cannot collide with its own input.
            output_path = (work / f"{stem}.{route.target}" if last
                           else work / f"{stem}.step{index + 1}.{route.target}")
            label = f"{route.source} -> {route.target}"
            if len(steps) > 1:
                label += f" (step {index + 1} of {len(steps)})"
            store.update(job_id, stage=label,
                         progress=5 + int(90 * index / len(steps)))
            route.handler(current, output_path)
            current = output_path

        store.update(job_id, status=DONE, progress=100, stage="complete",
                     output_path=current)
    except registry.UnsupportedConversion as exc:
        store.update(job_id, status=FAILED, error=exc.message)
    except ConversionError as exc:
        store.update(job_id, status=FAILED, error=exc.summary, details=exc.details)
    except FileNotFoundError as exc:
        # Raised by binaries.require() when a tool is missing.
        store.update(job_id, status=FAILED, error=str(exc))
    except Exception as exc:  # last resort: never leave a job stuck in "running"
        store.update(job_id, status=FAILED,
                     error=f"Unexpected error: {type(exc).__name__}",
                     details=repr(exc))


# ------------------------------------------------------------------- helpers

# Characters Windows forbids in a filename, plus the device names it reserves
# whatever the extension. A browser will happily send any of these.
_ILLEGAL = '<>:"/\\|?*'
_RESERVED = {"con", "prn", "aux", "nul",
             *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}


def safe_filename(raw: str) -> str:
    """A filename safe to build a path from, preserving the original where possible.

    The upload's name reaches the filesystem in three places (staging file, job
    input, output name), so it is sanitised once on the way in rather than
    trusted at each use.
    """
    name = Path(raw or "").name  # drop any directory component
    cleaned = "".join("_" if c in _ILLEGAL or ord(c) < 32 else c for c in name)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        return "upload"
    if Path(cleaned).stem.lower() in _RESERVED:
        cleaned = f"file_{cleaned}"
    # Leave room for the job directory and a ".stepN.ext" suffix on chains.
    if len(cleaned) > 120:
        stem, dot, ext = cleaned.rpartition(".")
        cleaned = (stem[:100] + dot + ext[:16]) if dot else cleaned[:120]
    return cleaned


async def save_upload(upload: UploadFile, destination: Path) -> int:
    """Stream an upload to disk, refusing anything over the size cap.

    Streaming matters: the cap has to be enforced before a 4 GB file has been
    buffered into memory.
    """
    written = 0
    with open(destination, "wb") as out:
        while chunk := await upload.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                out.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File is larger than the {MAX_UPLOAD_MB} MB limit.",
                )
            out.write(chunk)
    return written


# -------------------------------------------------------------------- routes

@app.get("/health")
def health() -> dict:
    report = binaries.health()
    missing = [t["display"] for t in report.values() if not t["present"]]
    return {
        "ok": not missing,
        "tools": report,
        "missing": missing,
        "max_upload_mb": MAX_UPLOAD_MB,
    }


@app.get("/supported")
def supported() -> dict:
    report = binaries.health()
    return {
        "matrix": registry.matrix(),
        "tools_present": {k: v["present"] for k, v in report.items()},
    }


@app.post("/convert")
async def convert(background: BackgroundTasks,
                  file: UploadFile = File(...),
                  target_format: str | None = Form(None)) -> JSONResponse:
    filename = safe_filename(file.filename or "upload")
    staging = jobs.TEMP_DIR / f"upload_{os.urandom(6).hex()}_{filename}"
    jobs.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    size = await save_upload(file, staging)

    info = detect(staging, filename)
    suggested = registry.targets_for(info.ext)

    # No target chosen yet: report what the file actually is and what it can
    # become. The caller comes back with a second request to start the job.
    if not target_format:
        staging.unlink(missing_ok=True)
        return JSONResponse({
            "job_id": None,
            "filename": filename,
            "size_bytes": size,
            "detected_type": info.description,
            "detected_ext": info.ext,
            "detected_mime": info.mime,
            "detection_source": info.source,
            "extension_mismatch": info.mismatch,
            "claimed_ext": info.claimed_ext,
            "suggested_targets": suggested,
            # Split so the picker can label the two-step routes honestly.
            "direct_targets": registry.direct_targets(info.ext),
            "chained_targets": registry.chained_targets(info.ext),
        })

    target = target_format.lower().lstrip(".")
    try:
        steps = registry.plan(info.ext, target)
    except registry.UnsupportedConversion as exc:
        staging.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail={
            "message": exc.message,
            "detected_type": info.description,
            "detected_ext": info.ext,
            "extension_mismatch": info.mismatch,
            "supported_targets": exc.alternatives,
        }) from None

    job = store.create(filename=filename, source_ext=info.ext, target_format=target,
                       detected_type=info.description, input_path=staging)
    # Move the upload into the job's own directory now that a job owns it.
    work = jobs.job_dir(job.id)
    owned = work / f"input_{filename}"
    shutil.move(str(staging), str(owned))
    store.update(job.id, input_path=owned)

    background.add_task(run_conversion, job.id)

    return JSONResponse({
        "job_id": job.id,
        "filename": filename,
        "size_bytes": size,
        "detected_type": info.description,
        "detected_ext": info.ext,
        "detected_mime": info.mime,
        "detection_source": info.source,
        "extension_mismatch": info.mismatch,
        "claimed_ext": info.claimed_ext,
        "target_format": target,
        "suggested_targets": suggested,
        "steps": [f"{s.source}->{s.target}" for s in steps],
        "chained": len(steps) > 1,
    })


@app.get("/status/{job_id}")
def status(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    return job.public()


@app.get("/download/{job_id}")
def download(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    if job.status != DONE or not job.output_path:
        raise HTTPException(status_code=409,
                            detail=f"Job is {job.status}, not ready to download.")
    path = Path(job.output_path)
    if not path.exists():
        raise HTTPException(status_code=410,
                            detail="Converted file is no longer available.")
    return FileResponse(path, filename=job.output_name,
                        media_type="application/octet-stream")


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
