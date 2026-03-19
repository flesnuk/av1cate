import sys
import os
import csv
import subprocess
import argparse
import shlex
import traceback

def get_fps(video_file):
    """Gets the exact video FPS using ffprobe for accurate frame conversions"""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1", video_file
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    fps_str = result.stdout.strip()
    if not fps_str:
        return 24.0 # Fallback in case it fails
    num, den = fps_str.split('/')
    return float(num) / float(den)

def main():
    parser = argparse.ArgumentParser(description="Cut and transcode video segments to AV1 and audio to Opus.")
    parser.add_argument("-i", "--input", required=True, help="Path to the input video file (e.g., my_video.mp4)")
    parser.add_argument("-o", "--output", help="Name of the final output MKV file", default=None)
    parser.add_argument("-p", "--params", help="Additional parameters for SvtAv1EncApp (e.g., '--preset 8 --crf 40')", default="")
    parser.add_argument("--opus-bitrate", help="Opus VBR bitrate in kbps (default 128)", default="128")
    parser.add_argument("-r", "--range", help="Frame ranges (e.g., 50-120,200-500). Overrides CSV files.", default=None)
    parser.add_argument("--range-file", help="CSV file containing frame numbers instead of timestamps", default=None)
    parser.add_argument("--timestamps-file", help="Override default timestamps CSV file", default=None)
    
    args = parser.parse_args()
    video_file = args.input
    
    if not os.path.exists(video_file):
        print(f"Error: Input video file not found: {video_file}")
        sys.exit(1)
        
    fps = get_fps(video_file)
    print(f"[*] Detected video FPS: {fps}")
    
    # Determine the final output filename
    if args.output:
        final_output = args.output
    else:
        base_name = video_file.rsplit('.', 1)[0]
        final_output = f"{base_name}_final_av1.mkv"
    
    # Safely parse the extra SvtAv1EncApp parameters into a list
    extra_params = shlex.split(args.params) if args.params else []
    
    # Initialize lists for temp files so they are accessible in the finally block
    segments_video = []
    segments_audio = []
    merged_video = "temp_merged_video.mkv"
    merged_audio = "temp_merged_audio.opus"
    concat_list = "temp_audio_list.txt"
    
    try:
        segments_data = [] # Will store tuples of (start_time, end_time, start_frame, end_frame)
        
        # Priority 1: Command line ranges
        if args.range:
            print("[*] Using command-line frame ranges.")
            ranges = args.range.split(',')
            for r in ranges:
                start_f, end_f = map(int, r.split('-'))
                segments_data.append((start_f / fps, end_f / fps, start_f, end_f))
                
        # Priority 2: CSV with frame numbers
        elif args.range_file:
            print(f"[*] Using frame ranges from CSV: {args.range_file}")
            with open(args.range_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    start_f = int(row['Start'])
                    end_f = int(row['End'])
                    segments_data.append((start_f / fps, end_f / fps, start_f, end_f))
                    
        # Priority 3 & 4: CSV with timestamps
        else:
            csv_file = args.timestamps_file if args.timestamps_file else video_file + ".csv"
            print(f"[*] Using timestamps from CSV: {csv_file}")
            if not os.path.exists(csv_file):
                print(f"Error: CSV file not found: {csv_file}")
                sys.exit(1)
            with open(csv_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    start_t = float(row['Start'])
                    end_t = float(row['End'])
                    segments_data.append((start_t, end_t, round(start_t * fps), round(end_t * fps)))

        if not segments_data:
            print("Error: No segments found to process.")
            sys.exit(1)

        # Process each segment
        for i, (start_time, end_time, start_frame, end_frame) in enumerate(segments_data):
            num_frames = end_frame - start_frame
            
            # 1. Extract and Encode Video
            ivf_out = f"temp_seg_{i}.ivf"
            print(f"\n[*] Encoding video segment {i} (Start Frame: {start_frame}, Total Frames: {num_frames})")
            cmd_video = [
                "SvtAv1EncApp", "-i", video_file, "-b", ivf_out,
                "--skip", str(start_frame), "--frames", str(num_frames)
            ] + extra_params
            subprocess.run(cmd_video, check=True)
            segments_video.append(ivf_out)
            
            # 2. Extract Audio to FLAC
            flac_out = f"temp_seg_{i}.flac"
            print(f"[*] Extracting audio segment {i} (From {start_time:.3f}s to {end_time:.3f}s)")
            duration = end_time - start_time
            cmd_audio = [
                "ffmpeg", "-y", "-ss", str(start_time), "-i", video_file,
                "-t", str(duration), "-vn", "-c:a", "flac", flac_out
            ]
            subprocess.run(cmd_audio, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            segments_audio.append(flac_out)
            
        # 3. Concatenate video fragments
        print("\n[*] Concatenating video fragments...")
        cmd_mkvmerge = ["mkvmerge", "-o", merged_video, segments_video[0]]
        for seg in segments_video[1:]:
            cmd_mkvmerge.extend(["+", seg])
        subprocess.run(cmd_mkvmerge, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # 4. Concatenate and transcode audio to Opus
        opus_kbps = f"{args.opus_bitrate}k"
        print(f"[*] Concatenating audio and transcoding to Opus ({opus_kbps} VBR)...")
        with open(concat_list, 'w', encoding='utf-8') as f:
            for seg in segments_audio:
                f.write(f"file '{seg}'\n")
                
        cmd_audio_concat = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c:a", "libopus", "-b:a", opus_kbps, "-vbr", "on", merged_audio
        ]
        subprocess.run(cmd_audio_concat, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # 5. Final Muxing
        print(f"\n[*] Muxing streams into final file: {final_output}")
        cmd_mux = [
            "mkvmerge", "-o", final_output, merged_video, merged_audio
        ]
        subprocess.run(cmd_mux, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        print(f"\n[+] Process completed successfully! Output saved to: {final_output}")

    except Exception as e:
        print("\n[!] An error occurred during processing.")
        print(traceback.format_exc())
        print("[!] Proceeding to clean up generated temporary files...")

    finally:
        # 6. Cleanup temporary files regardless of success or failure
        print("\n[*] Cleaning up temporary files...")
        temporales = segments_video + segments_audio + [merged_video, merged_audio, concat_list]
        for temp_file in temporales:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception as cleanup_error:
                    print(f"[-] Could not remove temp file {temp_file}: {cleanup_error}")
        print("[*] Cleanup finished.")

if __name__ == "__main__":
    main()
