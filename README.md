# av1cate

**AV1 encoding queue manager** with a web UI and segment-based encoding.

Built on **FastAPI** + **SvtAv1EncApp** + **mkvtoolnix** + **ffmpeg**.

**SvtAv1EncApp** needs to be compiled with FFMS2 support and for segment-based encoding, it needs to use `--skip` and `--frames` flags.

---

## Project Structure

```
av1cate/
├── api/
│   ├── __init__.py
│   └── main.py          ← FastAPI app, job queue, endpoints
├── core/
│   ├── __init__.py
│   └── av1kut.py        ← Segment encoding module (importable + CLI)
├── frontend/
│   └── index.html       ← Web UI (Vercel-style dark design)
├── run.py               ← Server launcher
├── requirements.txt
└── README.md
```

> **Legacy files** `av1_encoder_api.py` and `av1-kut.py` are kept in the root as reference. They are not imported by the new code.

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| Python ≥ 3.10 | Runtime |
| `SvtAv1EncApp` | AV1 video encoder |
| `ffmpeg` / `ffprobe` | Audio extraction, FPS detection |
| `mkvmerge` / `mkvextract` | Muxing + timecodes (from mkvtoolnix) |
| `mkvpropedit` | Embed encoder metadata in MKV |

---

## Installation

```bash
# 1. Clone / enter the project
cd av1cate

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Start the server
python run.py
```

Open **http://localhost:8000** in your browser.

Optional flags:
```bash
python run.py --port 9000 --reload   # dev mode with auto-reload
```

---

## How It Works

### Encoding Logic

When a job is dequeued for processing the API checks whether a **CSV file** exists alongside the input video:

```
/videos/episode.mp4          ← input video
/videos/episode.mp4.csv      ← timestamps CSV  ← if this exists → segment mode
```

#### Mode A — Segment encoding (`av1kut`, CSV present)

Uses `core/av1kut.py` to encode only the frame ranges defined in the CSV:

1. Parse CSV → list of `(start_time_s, end_time_s, start_frame, end_frame)` tuples
2. For each segment:
   - Encode video with `SvtAv1EncApp --skip <frame> --frames <count>`
   - Extract audio slice with `ffmpeg`
3. Concatenate all video segments → `mkvmerge`
4. Concatenate all audio slices → `ffmpeg` → Opus
5. Final mux → output MKV

**Output filename:** `<stem>_kut_<preset>P<crf>Q.mkv`

#### Mode B — Full-file encoding (standard, no CSV)

Encodes the entire video in one pass:

1. `SvtAv1EncApp` → `.ivf` (video)
2. `ffmpeg` → `.opus` (audio, optional)
3. `mkvextract` → timecodes (if MKV source)
4. `mkvmerge` → final MKV
5. `mkvpropedit` → write ENCODER / ENCODER_SETTINGS metadata tags

**Output filename:** `<stem>_<preset>P<crf>Q.mkv`

---

## CSV Format

The timestamps CSV must have `Start` and `End` columns (values in **seconds**):

```csv
Start,End
12.500,45.000
102.000,178.300
310.000,420.000
```

Save it as `<exactly_the_video_filename>.csv`, e.g.:

```
episode.mp4   →  episode.mp4.csv
movie.mkv     →  movie.mkv.csv
```

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/` | Serve the web UI |
| `POST` | `/api/jobs` | Create and enqueue a job |
| `GET`  | `/api/jobs` | List all jobs + queue state |
| `DELETE`| `/api/jobs/{id}` | Remove a job (not while processing) |
| `POST` | `/api/control/play` | Start queue processing |
| `POST` | `/api/control/stop` | Stop queue, kill active process, clean temps |
| `GET`  | `/api/browse?path=` | Browse server filesystem for video files |

### Job payload (`POST /api/jobs`)

```json
{
  "input_path":     "/absolute/path/to/video.mp4",
  "preset":         4,
  "crf":            35,
  "tune":           0,
  "encode_opus":    false,
  "opus_quality":   128,
  "optional_params": "--film-grain 4"
}
```

> `encode_opus` and `opus_quality` apply only to **standard mode**. In segment mode, audio is always converted to Opus using `opus_quality`.

---

## Using `av1kut` as a Standalone CLI

```bash
# Encode segments defined in a timestamps CSV
python -m core.av1kut -i video.mp4 -p "--preset 6 --crf 32"

# Specify frame ranges directly
python -m core.av1kut -i video.mp4 -r "50-300,800-1200" -p "--preset 4 --crf 35"

# Use a separate frames CSV (Start/End in frame numbers)
python -m core.av1kut -i video.mp4 --range-file cuts.csv

# Custom output path and Opus bitrate
python -m core.av1kut -i video.mp4 -o output.mkv --opus-bitrate 192
```

---

## Web UI

The interface (`frontend/index.html`) is served directly by FastAPI.

**Features:**
- Play / Stop queue controls with live status indicator
- Add job form — preset, CRF, tune, Opus toggle, extra params
- Filesystem browser modal for picking video files
- Job queue table showing mode (`standard` / `segments`), progress, final summary
- Auto-refresh every 3 s
- Inline CSV format reference card

**Design:** Vercel/midnight aesthetic — pure black background, Inter + JetBrains Mono typography, outlined buttons with thin white borders, status badges.
