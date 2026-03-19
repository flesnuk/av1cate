"""
av1kut — AV1 segment-based encoding core module.

Can be used as:
  - An importable library from the API: call process_segments()
  - A standalone CLI tool: python -m core.av1kut -i video.mp4 ...
"""

import os
import csv
import asyncio
import shlex
import time
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Type alias for segment data
# (start_time_s, end_time_s, start_frame, end_frame, name)
# ---------------------------------------------------------------------------
Segment = Tuple[float, float, int, int, str]


# ---------------------------------------------------------------------------
# FPS detection
# ---------------------------------------------------------------------------

def get_fps(video_file: str) -> float:
    """
    Returns the exact video FPS using ffprobe.
    Falls back to 24.0 if detection fails.
    """
    import subprocess
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1", video_file
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    fps_str = result.stdout.strip()
    if not fps_str:
        return 24.0
    num, den = fps_str.split('/')
    return float(num) / float(den)


# ---------------------------------------------------------------------------
# CSV parsing helpers
# ---------------------------------------------------------------------------

def load_segments_from_timestamps_csv(csv_path: str, fps: float) -> List[Segment]:
    """
    Parse a timestamps CSV with 'Start' and 'End' columns (in seconds).
    """
    segments: List[Segment] = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            start_t = float(row['Start'])
            end_t = float(row['End'])
            name_t = row['Name']
            segments.append((start_t, end_t, round(start_t * fps), round(end_t * fps), name_t))
    return segments


def load_segments_from_frames_csv(csv_path: str, fps: float) -> List[Segment]:
    """
    Parse a frames CSV with 'Start' and 'End' columns (in frame numbers).
    """
    segments: List[Segment] = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            start_f = int(row['Start'])
            end_f = int(row['End'])
            name_t = row['Name']
            segments.append((start_f / fps, end_f / fps, start_f, end_f, name_t))
    return segments


def csv_exists_for_video(video_path: str) -> Optional[str]:
    """
    Returns the path of the default timestamps CSV for a given video,
    or None if it does not exist.
    The convention is: <video_file>.csv (e.g. my_video.mp4.csv)
    """
    candidate = video_path + ".csv"
    return candidate if os.path.exists(candidate) else None


# ---------------------------------------------------------------------------
# Async subprocess helper
# ---------------------------------------------------------------------------

async def _run_cmd(cmd: List[str], capture_stderr: bool = False) -> None:
    """
    Run a shell command asynchronously.
    Raises RuntimeError on non-zero exit code.
    """
    stderr_target = asyncio.subprocess.PIPE if capture_stderr else asyncio.subprocess.DEVNULL
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=stderr_target,
    )
    _, stderr_data = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr_data.decode(errors='ignore') if stderr_data else ""
        raise RuntimeError(
            f"Command failed (code {proc.returncode}): {' '.join(cmd)}\n{msg}"
        )

def append_line_to_log_file(log_file: str, line: str) -> None:
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("\n" + line)

def merge_video_params(base_params: List[str], overrides_str: str) -> List[str]:
    """
    Merges base SvtAv1EncApp parameters with an override string.
    Overrides from the string take precedence.
    """
    if not overrides_str or not str(overrides_str).strip():
        return list(base_params)
        
    overrides = shlex.split(str(overrides_str))
    
    def parse_params(params: List[str]) -> dict:
        parsed = {}
        i = 0
        while i < len(params):
            p = params[i]
            if p.startswith('-'):
                # Check if next element is a value (e.g. doesn't start with '-' followed by letter)
                is_value_next = (
                    i + 1 < len(params) and 
                    not (params[i+1].startswith('-') and len(params[i+1]) > 2 and params[i+1][2].isalpha())
                )
                if is_value_next:
                    parsed[p] = params[i+1]
                    i += 2
                else:
                    parsed[p] = None
                    i += 1
            else:
                parsed[p] = None
                i += 1
        return parsed

    base_dict = parse_params(base_params)
    over_dict = parse_params(overrides)
    
    base_dict.update(over_dict)
    
    result = []
    for k, v in base_dict.items():
        if k.startswith('-'):
            result.append(k)
            if v is not None:
                result.append(v)
            
    return result

# ---------------------------------------------------------------------------
# Core processing function (async)
# ---------------------------------------------------------------------------

async def process_segments(
    video_file: str,
    segments_data: List[Segment],
    extra_params: Optional[List[str]] = None,
    opus_bitrate: str = "128",
    output_path: Optional[str] = None,
    work_dir: Optional[str] = None,
    log_file: Optional[str] = None,
) -> str:
    """
    Encode the specified segments of a video to AV1 + Opus and mux into an MKV.

    Parameters
    ----------
    video_file    : Absolute path to the source video.
    segments_data : List of (start_time_s, end_time_s, start_frame, end_frame).
    extra_params  : Additional SvtAv1EncApp flags, already split into a list.
    opus_bitrate  : Opus VBR target in kbps (default "128").
    output_path   : Destination MKV path. Auto-generated if None.
    work_dir      : Directory for temp files. Defaults to source video's directory.
    log_file      : Path to the log file. Auto-generated if None.

    Returns
    -------
    str — path to the final MKV file.

    Raises
    ------
    RuntimeError on encoding / muxing failure.
    ValueError   if segments_data is empty.
    """
    if not segments_data:
        raise ValueError("No segments provided to process_segments().")

    extra_params = extra_params or []
    video_path = Path(video_file)

    if work_dir is None:
        work_dir = str(video_path.parent)
    wd = Path(work_dir)

    if output_path is None:
        base_name = video_path.stem
        output_path = str(wd / f"{base_name}_kut_av1.mkv")

    # Temp file paths (relative to work_dir)
    def tmp(name: str) -> str:
        return str(wd / name)

    segments_video: List[str] = []
    segments_audio: List[str] = []
    merged_video = tmp("_kut_merged_video.mkv")
    merged_audio = tmp("_kut_merged_audio.opus")
    concat_list  = tmp("_kut_audio_list.txt")

    try:
        for i, (start_time, end_time, start_frame, end_frame, name_t) in enumerate(segments_data):
            num_frames = end_frame - start_frame
            ivf_out  = tmp(f"_kut_seg_{i}.ivf")
            flac_out = tmp(f"_kut_seg_{i}.flac")

            append_line_to_log_file(log_file, f"Encoding segment {i+1}/{len(segments_data)}: frame {start_frame} → {end_frame} ({num_frames} frames)" )

            # use name_t as a override params
            # so for example if it has --crf 35, it would replace the original 
            # --crf 40 from the extra params
            cmd_video_params = extra_params
            if name_t and name_t.strip():
                cmd_video_params = merge_video_params(extra_params, name_t)

            # 1. Encode video segment
            print(f"  [kut] Encoding segment {i}: frame {start_frame} → {end_frame} ({num_frames} frames)")
            cmd_video = [
                "SvtAv1EncApp", "-i", video_file, "-b", ivf_out,
                "--skip", str(start_frame), "--frames", str(num_frames),
            ] + cmd_video_params
            print(f"  [kut] Command: {' '.join(cmd_video)}")
            segments_video.append(ivf_out)
            await _run_cmd(cmd_video)

            # 2. Extract audio segment
            duration = end_time - start_time
            print(f"  [kut] Extracting audio segment {i}: {start_time:.3f}s → {end_time:.3f}s")
            cmd_audio = [
                "ffmpeg", "-y",
                "-ss", str(start_time), "-i", video_file,
                "-t", str(duration), "-vn", "-c:a", "flac", flac_out,
            ]
            await _run_cmd(cmd_audio)
            segments_audio.append(flac_out)

        # 3. Concatenate video fragments
        print("  [kut] Concatenating video segments...")
        cmd_mkvmerge = ["mkvmerge", "-o", merged_video, segments_video[0]]
        for seg in segments_video[1:]:
            cmd_mkvmerge.extend(["+", seg])
        await _run_cmd(cmd_mkvmerge)

        # 4. Concatenate audio + transcode to Opus
        opus_kbps = f"{opus_bitrate}k"
        print(f"  [kut] Merging audio → Opus ({opus_kbps} VBR)...")
        with open(concat_list, 'w', encoding='utf-8') as f:
            for seg in segments_audio:
                f.write(f"file '{seg}'\n")
        cmd_audio_concat = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c:a", "libopus", "-b:a", opus_kbps, "-vbr", "on", merged_audio,
        ]
        await _run_cmd(cmd_audio_concat)

        # 5. Final mux
        print(f"  [kut] Muxing → {output_path}")
        await _run_cmd(["mkvmerge", "-o", output_path, merged_video, merged_audio])

        print(f"  [kut] Done! Output: {output_path}")

        # delete log file
        if log_file and os.path.exists(log_file):
            os.remove(log_file)
        return output_path

    finally:
        # 6. Cleanup all temp files
        print("  [kut] Cleaning up temp files...")
        time.sleep(0.5) # sleep a bit to ensure async process finishes
        all_temps = segments_video + segments_audio + [merged_video, merged_audio, concat_list]
        if log_file and os.path.exists(log_file):
            os.remove(log_file)
        for temp_file in all_temps:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception as e:
                print(f"  [kut] Warning: could not remove {temp_file}: {e}")


# ---------------------------------------------------------------------------
# Standalone CLI entry point  (python -m core.av1kut  or  python av1kut.py)
# ---------------------------------------------------------------------------

def _cli_main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="AV1 segment cutter: encode specific frame ranges to AV1+Opus MKV."
    )
    parser.add_argument("-i", "--input",    required=True,  help="Source video file")
    parser.add_argument("-o", "--output",   default=None,   help="Output MKV filename")
    parser.add_argument("-p", "--params",   default="",     help="Extra SvtAv1EncApp params (quoted string)")
    parser.add_argument("--opus-bitrate",   default="128",  help="Opus VBR kbps (default 128)")
    parser.add_argument("-r", "--range",    default=None,   help="Frame ranges: 50-120,200-500")
    parser.add_argument("--range-file",     default=None,   help="CSV with Start/End frame columns")
    parser.add_argument("--timestamps-file",default=None,   help="CSV with Start/End timestamp columns (seconds)")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: input file not found: {args.input}")
        sys.exit(1)

    fps = get_fps(args.input)
    print(f"[*] FPS: {fps}")

    extra_params = shlex.split(args.params) if args.params else []
    segments_data: List[Segment] = []

    if args.range:
        for r in args.range.split(','):
            s, e = map(int, r.split('-'))
            segments_data.append((s / fps, e / fps, s, e))
    elif args.range_file:
        segments_data = load_segments_from_frames_csv(args.range_file, fps)
    else:
        csv_file = args.timestamps_file or (args.input + ".csv")
        if not os.path.exists(csv_file):
            print(f"Error: CSV not found: {csv_file}")
            sys.exit(1)
        segments_data = load_segments_from_timestamps_csv(csv_file, fps)

    if not segments_data:
        print("Error: no segments found.")
        sys.exit(1)

    result = asyncio.run(
        process_segments(
            video_file=args.input,
            segments_data=segments_data,
            extra_params=extra_params,
            opus_bitrate=args.opus_bitrate,
            output_path=args.output,
        )
    )
    print(f"\n[+] Completed: {result}")


if __name__ == "__main__":
    _cli_main()
