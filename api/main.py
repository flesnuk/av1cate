"""
AV1 Encoder API — FastAPI backend.

Logic for job processing:
  - If <input_video>.csv exists → uses core.av1kut.process_segments()
                                   (segment-based AV1 cut/encode)
  - Otherwise → full-file SvtAv1EncApp encode pipeline (standard flow)

Run with:  python run.py
           OR: uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import shlex
import uuid
import re
import time
from pathlib import Path
from typing import List, Optional, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import av1kut


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class JobStatus:
    PENDING    = "pending"
    PROCESSING = "processing"
    COMPLETED  = "completed"
    ERROR      = "error"


class JobCreate(BaseModel):
    input_path:     str = Field(...,          description="Full path of the mp4/mkv video")
    preset:         int = Field(default=4,    ge=1,  le=10)
    crf:            int = Field(default=35,   ge=10, le=60)
    optional_params:str = Field(default="",  description="Extra parameters for SvtAv1EncApp")
    encode_opus:    bool= Field(default=False,description="Convert audio to Opus (standard mode)")
    opus_quality:   int = Field(default=128,  description="Opus quality in kbps (standard mode)")
    tune:           int = Field(default=0,    ge=0,  le=5, description="Tune (0 a 5)")


class Job(JobCreate):
    id:               str
    status:           str           = JobStatus.PENDING
    error_message:    Optional[str] = None
    log_file:         Optional[str] = None
    current_progress: Optional[str] = None
    final_summary:    Optional[str] = None
    mode:             str           = "standard"   # "standard" | "segments"
    start_time:       Optional[float] = None
    end_time:         Optional[float] = None


# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------

jobs_db:   Dict[str, Job] = {}
queue:     List[str]      = []

is_running:       bool                              = False
current_process:  Optional[asyncio.subprocess.Process] = None
current_job_id:   Optional[str]                    = None

ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_kut_progress(log_file: str) -> str:
    """Read the tail of the SVT-AV1 log and return the last progress line."""
    try:
        with open(log_file, "rb") as f:
            lines = f.readlines()
            lines = [line for line in lines if line.strip()]
            if lines:
                return lines[-1].decode('utf-8', errors='ignore')
    except Exception:
        pass
    return ""

def get_last_progress(log_file: str) -> str:
    """Read the tail of the SVT-AV1 log and return the last progress line."""
    try:
        with open(log_file, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size > 2048:
                f.seek(size - 2048)
            else:
                f.seek(0)
            content = f.read().decode('utf-8', errors='ignore')
            lines = content.replace('\n', '\r').split('\r')
            for line in reversed(lines):
                if "Encoding:" in line:
                    clean_line = ANSI_ESCAPE.sub('', line)
                    return " ".join(clean_line.split())
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Subprocess helper (standard flow)
# ---------------------------------------------------------------------------

async def run_command(cmd: List[str], log_file: Optional[str] = None):
    """Execute a system command, optionally streaming stderr to a log file."""
    global current_process, is_running

    if not is_running:
        raise InterruptedError("Cancelled by user (Stop).")

    print(f"  [cmd] {' '.join(cmd)}")

    f_log = None
    try:
        if log_file:
            f_log = open(log_file, "wb")
            current_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=f_log,
            )
            await current_process.wait()
        else:
            current_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await current_process.communicate()

        if current_process.returncode != 0:
            if not is_running:
                raise InterruptedError("Cancelled by user (Stop).")
            err_msg = "Unknown error"
            if not log_file and stderr:
                err_msg = stderr.decode(errors='ignore')
            elif log_file:
                err_msg = f"Command failed. Check log: {log_file}"
            raise RuntimeError(f"Failed with code {current_process.returncode}: {err_msg}")
    finally:
        if f_log:
            f_log.close()


# ---------------------------------------------------------------------------
# Job Processing
# ---------------------------------------------------------------------------

async def process_job(job: Job):
    """
    Dispatch job to the correct encode pipeline:
      • segments mode → core.av1kut.process_segments()
      • standard mode → full-file SvtAv1EncApp + ffmpeg + mkvmerge
    """
    input_path = Path(job.input_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"File {input_path} does not exist.")

    # Check if a CSV exists alongside the input video
    csv_candidate = av1kut.csv_exists_for_video(str(input_path))

    if csv_candidate:
        await _process_job_segments(job, input_path, csv_candidate)
    else:
        await _process_job_standard(job, input_path)


async def _process_job_segments(job: Job, input_path: Path, csv_path: str):
    """
    Segment-based encode via core.av1kut.
    Uses the timestamps CSV found next to the video.
    """
    job.mode = "segments"
    print(f"[job:{job.id}] CSV found ({csv_path}) → segments mode (av1kut)")

    fps = av1kut.get_fps(str(input_path))
    segments_data = av1kut.load_segments_from_timestamps_csv(csv_path, fps)
    
    base_name  = input_path.stem
    dir_name   = input_path.parent
    log_txt    = dir_name / f"{base_name}_temp.log"

    job.log_file = str(log_txt)

    if not segments_data:
        raise ValueError(f"CSV {csv_path} does not contain valid segments.")

    extra_params = shlex.split(job.optional_params) if job.optional_params else []
    # Inject preset / crf / tune into SvtAv1EncApp params for the kut pipeline
    svt_base = [
        "--preset", str(job.preset),
        "--crf",    str(job.crf),
        "--tune",   str(job.tune),
        "--color-primaries", "1",
        "--transfer-characteristics", "1",
        "--matrix-coefficients", "1",
    ]

    base_name = input_path.stem
    output_path = str(input_path.parent / f"{base_name}_kut_{job.preset}P{job.crf}Q.mkv")

    output = await av1kut.process_segments(
        video_file=str(input_path),
        segments_data=segments_data,
        extra_params=svt_base + extra_params,
        opus_bitrate=str(job.opus_quality),
        output_path=output_path,
        work_dir=str(input_path.parent),
        log_file=str(log_txt),
    )

    job.final_summary = f"Segments: {len(segments_data)} | Output: {Path(output).name}"


async def _process_job_standard(job: Job, input_path: Path):
    """
    Full-file encode pipeline: SvtAv1EncApp → ffmpeg (audio) → mkvmerge → mkvpropedit.
    """
    job.mode = "standard"
    print(f"[job:{job.id}] No CSV → standard mode")

    base_name  = input_path.stem
    dir_name   = input_path.parent
    video_ivf  = dir_name / f"{base_name}_temp.ivf"
    audio_opus = dir_name / f"{base_name}_temp.opus"
    log_txt    = dir_name / f"{base_name}_temp.txt"
    timecodes  = dir_name / f"{base_name}_pts.txt"
    final_mkv  = dir_name / f"{base_name}_{job.preset}P{job.crf}Q.mkv"

    job.log_file = str(log_txt)

    # 1. Encode video
    cmd_svt = [
        "SvtAv1EncApp", "-i", str(input_path), "-b", str(video_ivf),
        "--preset", str(job.preset), "--crf", str(job.crf),
        "--tune", str(job.tune), "--progress", "2",
        "--color-primaries", "1", "--transfer-characteristics", "1",
        "--matrix-coefficients", "1",
    ]
    if job.optional_params:
        cmd_svt.extend(shlex.split(job.optional_params))
    await run_command(cmd_svt, log_file=str(log_txt))
    job.final_summary = get_last_progress(str(log_txt))

    # 2. Encode audio (optional)
    if job.encode_opus:
        cmd_audio = [
            "ffmpeg", "-y", "-i", str(input_path), "-vn",
            "-c:a", "libopus", "-b:a", f"{job.opus_quality}k", "-vbr", "on",
            str(audio_opus),
        ]
        await run_command(cmd_audio)

    # 3. Extract original timecodes (MKV source)
    has_timecodes = False
    try:
        cmd_pts = ["mkvextract", str(input_path), "timecodes_v2", f"0:{timecodes}"]
        proc_pts = await asyncio.create_subprocess_exec(
            *cmd_pts,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc_pts.wait()
        if proc_pts.returncode == 0 and timecodes.exists() and timecodes.stat().st_size > 0:
            has_timecodes = True
    except Exception:
        pass

    # 4. Mux
    cmd_mux = ["mkvmerge", "-o", str(final_mkv)]
    if has_timecodes:
        cmd_mux.extend(["--timecodes", f"0:{timecodes}"])
    if job.encode_opus:
        cmd_mux.extend([str(video_ivf), str(audio_opus)])
    else:
        cmd_mux.extend([str(video_ivf), "--no-video", str(input_path)])
    await run_command(cmd_mux)

    # 5. Add metadata
    try:
        proc_ver = await asyncio.create_subprocess_exec(
            "SvtAv1EncApp", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc_ver.communicate()
        encoder_version = stdout.decode(errors='ignore').splitlines()[0].strip() if stdout else "SvtAv1EncApp"
    except Exception:
        encoder_version = "SvtAv1EncApp"

    video_params = f"--preset {job.preset} --crf {job.crf} --tune {job.tune}"
    if job.optional_params:
        video_params += f" {job.optional_params}"

    xml_file = dir_name / f"{base_name}_temp.xml"
    xml_file.write_text(f"""<?xml version="1.0"?>
<!-- <!DOCTYPE Tags SYSTEM "matroskatags.dtd"> -->
<Tags>
  <Tag><Simple><Name></Name><String></String></Simple></Tag>
  <Tag>
    <Targets />
    <Simple><Name>ENCODER</Name><String>{encoder_version}</String></Simple>
    <Simple><Name>ENCODER_SETTINGS</Name><String>{video_params}</String></Simple>
  </Tag>
</Tags>""", encoding="utf-8")

    await run_command(["mkvpropedit", str(final_mkv), "--tags", f"track:v1:{xml_file}"])

    # 6. Cleanup
    for f in [video_ivf, audio_opus, log_txt, xml_file, timecodes]:
        if f.exists():
            f.unlink()


# ---------------------------------------------------------------------------
# Worker Loop
# ---------------------------------------------------------------------------

async def worker_loop():
    global is_running, current_process, current_job_id

    while True:
        if is_running and queue:
            job_id = queue[0]
            current_job_id = job_id
            job = jobs_db[job_id]
            job.status = JobStatus.PROCESSING
            job.start_time = time.time()
            job.error_message = None

            try:
                await process_job(job)
                if job.status == JobStatus.PROCESSING:
                    job.status = JobStatus.COMPLETED
                    job.end_time = time.time()
                    queue.pop(0)
            except InterruptedError:
                job.status = JobStatus.PENDING
                job.start_time = None
                print(f"[job:{job_id}] Interrupted → pending.")
            except Exception as e:
                job.status = JobStatus.ERROR
                job.end_time = time.time()
                job.error_message = str(e)
                queue.pop(0)
                print(f"[job:{job_id}] Error: {e}")
            finally:
                current_process = None
                current_job_id  = None

        await asyncio.sleep(1)


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(worker_loop())
    yield
    task.cancel()


app = FastAPI(title="AV1 Encoder API", version="2.0.0", lifespan=lifespan)

# Serve frontend static files
_frontend_dir = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
def get_dashboard():
    return FileResponse(str(_frontend_dir / "index.html"))


@app.post("/api/jobs", response_model=Job)
def create_job(job_in: JobCreate):
    """Enqueue a new encoding job."""
    job_id  = str(uuid.uuid4())[:8]
    new_job = Job(id=job_id, **job_in.dict())
    jobs_db[job_id] = new_job
    queue.append(job_id)
    return new_job


@app.get("/api/jobs")
def get_jobs():
    """Return all jobs and global queue state."""
    for job in jobs_db.values():
        if job.status == JobStatus.PROCESSING and job.log_file:
            if job.log_file.endswith('log'):
                job.current_progress = get_kut_progress(job.log_file)
            else:
                job.current_progress = get_last_progress(job.log_file)
    return {
        "is_running": is_running,
        "queue":      queue,
        "jobs":       list(jobs_db.values()),
    }


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    if job_id not in jobs_db:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs_db[job_id]
    if job.status == JobStatus.PROCESSING:
        raise HTTPException(status_code=400, detail="To stop an ongoing job, press Stop first.")
    if job_id in queue:
        queue.remove(job_id)
    del jobs_db[job_id]
    return {"message": "Job deleted"}


@app.post("/api/control/play")
def play_queue():
    global is_running
    is_running = True
    return {"message": "Queue started"}


@app.post("/api/control/stop")
async def stop_queue():
    global is_running, current_process, current_job_id
    is_running = False

    if current_process:
        try:
            current_process.terminate()
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"Error terminating process: {e}")

    if current_job_id and current_job_id in jobs_db:
        job = jobs_db[current_job_id]
        input_path = Path(job.input_path)
        base_name  = input_path.stem
        dir_name   = input_path.parent
        temps = [
            dir_name / f"{base_name}_temp.ivf",
            dir_name / f"{base_name}_temp.opus",
            dir_name / f"{base_name}_temp.txt",
            dir_name / f"{base_name}_temp.xml",
            dir_name / f"{base_name}_pts.txt",
        ]
        for t in temps:
            if t.exists():
                try:
                    t.unlink()
                except Exception as e:
                    print(f"Could not delete {t}: {e}")
        job.status = JobStatus.PENDING
        job.start_time = None

    return {"message": "Queue stopped. Temp files deleted and task reset."}


@app.get("/api/browse")
def browse_fs(path: Optional[str] = None):
    """Navigate the server filesystem to pick video files."""
    if not path:
        path = "/"
    p = Path(path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=400, detail="Invalid directory or does not exist")

    items = []
    if p.parent != p:
        items.append({"name": "..", "path": str(p.parent), "is_dir": True})
    try:
        for entry in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.is_dir() and not entry.name.startswith('.'):
                items.append({"name": entry.name, "path": str(entry), "is_dir": True})
            elif entry.suffix.lower() in {'.mp4', '.mkv', '.avi', '.webm', '.mov', '.ts'}:
                items.append({"name": entry.name, "path": str(entry), "is_dir": False})
    except PermissionError:
        pass

    return {"current_path": str(p), "items": items}
