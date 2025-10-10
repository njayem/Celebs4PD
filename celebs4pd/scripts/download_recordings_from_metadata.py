#!/usr/bin/env python3
# download_recordings_from_metadata.py
# Author: Nadine El-Mufti (2025)

"""
Purpose
-------
Download recordings from deidentified metadata mapping and save one canonical WAV per item.
Writes a per-run summary and a retry-only mapping for failures.

Inputs
------
--mapping            : JSON file mapping keys to URLs
--out                : Output root directory for audio files
--cookies            : Optional path to cookies.txt file
--cookies-from-browser : Optional browser to extract cookies from
--force              : Force re-download of existing files
--user-agent         : Custom user agent string
--insecure-ssl       : Skip SSL certificate verification
--rate-limit         : Download rate limit (e.g., "1M" or "500K" for more stable connection)
--retries            : Number of download retries (default: 5, use 10+ for unstable connections)
--results-dir        : Directory for run summaries (default: ./results)
--filter-subjects    : Comma-separated subject IDs (e.g., "pd_01,pd_02,control_05")
--filter-recordings  : Comma-separated recording IDs (e.g., "recording_1,recording_3")
--filter-stages      : Comma-separated stages (e.g., "Pre-Diagnosis,Post-Diagnosis")
--filter-config      : Path to JSON config file for advanced filtering

Output
------
Downloaded WAV files organized by Group/Subject/[Stage]/recording_N.wav
Summary file and optional retry mapping for failed downloads.

Example
-------
# Download multiple subjects:
celebs4pd-download-data \
  --mapping "./mapping.json" \
  --out "./Celebs4PD Dataset" \
  --cookies "./cookies.txt" \
  --filter-subjects "pd_01,pd_02,pd_03"

# Advanced filtering with config file:
celebs4pd-download-data \
  --mapping "./mapping.json" \
  --out "./Celebs4PD Dataset" \
  --cookies "./cookies.txt" \
  --filter-config "./filter_config.json"
"""

from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from shutil import which
from typing import Optional, List, Dict
from tqdm import tqdm

URL_FIELDS = ("source_url", "Source", "source", "URL", "url")


def _load_filter_config(config_path: Path) -> Dict:
    """Load filter configuration from JSON file."""
    if not config_path.exists():
        raise FileNotFoundError(f"Filter config not found: {config_path}")
    
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("Filter config must be a JSON object")
        
        for subject_id, stages in config.items():
            if stages is not None and not isinstance(stages, list):
                raise ValueError(f"Invalid config for {subject_id}: stages must be a list or null")
        
        return config
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in filter config: {e}")


# ────────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────────

def _mapping_value_to_url(v) -> str:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        for k in URL_FIELDS:
            if v.get(k):
                return str(v[k]).strip()
    return ""


def _should_process_key(
    key: str, 
    filter_subjects: Optional[List[str]], 
    filter_recordings: Optional[List[str]], 
    filter_stages: Optional[List[str]],
    filter_config: Optional[Dict] = None
) -> bool:
    """Check if this key should be processed based on filters."""
    if not any([filter_subjects, filter_recordings, filter_stages, filter_config]):
        return True
    
    try:
        _, subject_id, stage, recording_id, _ = _parse_key_path(key)
        
        # Priority 1: Use filter_config if provided
        if filter_config:
            if subject_id not in filter_config:
                return False
            allowed_stages = filter_config[subject_id]
            if allowed_stages is not None and stage not in allowed_stages:
                return False
        else:
            if filter_subjects and subject_id not in filter_subjects:
                return False
            if filter_stages and stage not in filter_stages:
                return False
        
        if filter_recordings and recording_id not in filter_recordings:
            return False
        
        return True
    except Exception:
        return False


def _parse_key_path(key: str):
    key = key.replace("./Celebs4PD Dataset/", "").strip("/")
    
    parts = key.split("/")
    if len(parts) < 3:
        raise ValueError(f"Bad mapping key (need >=3 parts): {key}")

    group, subject_id = parts[0], parts[1]

    if len(parts) >= 4 and parts[2] in ("Pre-Diagnosis", "Post-Diagnosis"):
        stage = parts[2]
        recording_id = parts[3]
        rel = Path(group) / subject_id / stage
    else:
        stage = None
        recording_id = parts[2]
        rel = Path(group) / subject_id

    if not re.fullmatch(r"recording_\d+", recording_id):
        raise ValueError(f"Bad recording id in key: {key}")
    return group, subject_id, stage, recording_id, rel


def _recording_number(recording_id: str) -> int:
    return int(recording_id.split("_", 1)[1])


def _check_tools_or_die():
    if which("yt-dlp") is None:
        print("[ERROR] 'yt-dlp' not found. Install: mamba install -c conda-forge yt-dlp")
        sys.exit(3)
    if which("ffmpeg") is None:
        print("[ERROR] 'ffmpeg' not found. Install: mamba install -c conda-forge ffmpeg")
        sys.exit(3)


def _run_yt_dlp(
    url: str,
    out_dir: Path,
    cookies: Optional[str],
    cookies_from_browser: Optional[str],
    user_agent: str,
    insecure_ssl: bool,
    rate_limit: Optional[str],
    retries: int,
) -> float:
    """Run yt-dlp with tqdm progress bar; return elapsed seconds on success."""
    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "%(id)s.%(ext)s")

    base = [
            "yt-dlp",
            "--ignore-config",
            "--no-playlist",
            "--extract-audio",
            "--audio-format", "wav",
            "-o", output_template,
            "--user-agent", user_agent,
            "--retries", str(retries),
            "--socket-timeout", "15",
            "--add-header", "Referer:https://www.youtube.com/",
            "--add-header", "Accept-Language:en-US,en;q=0.9",
            "--add-header", "Accept:text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "--add-header", "Accept-Encoding:gzip, deflate, br",
            "--add-header", "Connection:keep-alive",
            "--newline",
        ]
        
    # Add source-specific headers
    if "youtube.com" in url or "youtu.be" in url:
         base.extend([
            "--sleep-requests", "1",
            "--sleep-interval", "2",
    ])
    if "dailymotion.com" in url:
        base.extend([
            "--add-header", "Referer:https://www.dailymotion.com/",
            "--add-header", "Origin:https://www.dailymotion.com",
        ])
    elif "npr.org" in url:
        base.extend([
            "--add-header", "Referer:https://www.npr.org/",
            "--add-header", "Origin:https://www.npr.org",
        ])
    elif "gala.fr" in url:
        base.extend([
            "--add-header", "Referer:https://www.gala.fr/",
            "--add-header", "Origin:https://www.gala.fr",
        ])
    elif "pastdaily.com" in url:
        base.extend([
            "--add-header", "Referer:https://pastdaily.com/",
            "--add-header", "Origin:https://pastdaily.com",
        ])

    strategies = [
        ["--extractor-args", "youtube:player_client=android,web", "--force-ipv4"],
        ["--extractor-args", "youtube:player_client=web", "--force-ipv4"],
        ["--extractor-args", "youtube:player_client=android,web"],
    ]

    start = time.time()
    last_rc = None
    last_err = ""

    for strat in strategies:
        cmd = base + strat + [url]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        pbar = None
        try:
            for line in proc.stdout:
                line = line.strip()
                if "[download]" in line and "%" in line:
                    match = re.search(r'(\d+\.?\d*)%', line)
                    if match:
                        percent = float(match.group(1))
                        if pbar is None:
                            pbar = tqdm(total=100, unit='%', bar_format='{desc}: {percentage:3.0f}%|{bar}| {n:.1f}/{total} [{elapsed}<{remaining}]')
                            pbar.set_description("download")
                        pbar.n = percent
                        pbar.refresh()
            
            proc.wait()
            if pbar:
                pbar.close()
            
            if proc.returncode == 0:
                return time.time() - start
            
            last_rc = proc.returncode
            last_err = proc.stderr.read() if proc.stderr else ""
        except KeyboardInterrupt:
            if pbar:
                pbar.close()
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise
        except Exception:
            if pbar:
                pbar.close()
            proc.kill()
            raise

    elapsed = time.time() - start
    raise RuntimeError(f"yt-dlp failed (exit {last_rc}, {elapsed:.1f}s)\n{last_err}")


def _newest_wav_in(dirpath: Path) -> Optional[Path]:
    wavs = list(dirpath.glob("*.wav"))
    if not wavs:
        return None
    wavs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return wavs[0]


def _process_one(
    mapping_key: str,
    url: str,
    out_root: Path,
    cookies: Optional[str],
    cookies_from_browser: Optional[str],
    force: bool,
    user_agent: str,
    insecure_ssl: bool,
    rate_limit: Optional[str],
    retries: int,
) -> tuple[bool, str, float]:
    try:
        _, _, _, recording_id, rel = _parse_key_path(mapping_key)
        rec_n = _recording_number(recording_id)

        dest_dir = out_root / rel
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"recording_{rec_n}.wav"

        if target.exists() and not force:
            return True, "existing", 0.0

        staging = dest_dir / ".staging"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        elapsed = _run_yt_dlp(
            url, staging, cookies, cookies_from_browser,
            user_agent, insecure_ssl, rate_limit, retries
        )

        wav = _newest_wav_in(staging)
        if not wav or not wav.exists():
            raise RuntimeError("No WAV produced by yt-dlp")

        shutil.move(str(wav), str(target))
        shutil.rmtree(staging, ignore_errors=True)
        return True, "downloaded", elapsed
    except Exception as e:
        return False, str(e), 0.0


# ────────────────────────────────────────────────────────────────────────────────
# Core
# ────────────────────────────────────────────────────────────────────────────────

def run(
    mapping: Path,
    out: Path,
    cookies: Optional[str],
    cookies_from_browser: Optional[str],
    force: bool,
    user_agent: str,
    insecure_ssl: bool,
    rate_limit: Optional[str],
    retries: int,
    results_dir: Path,
    filter_subjects: Optional[List[str]],
    filter_recordings: Optional[List[str]],
    filter_stages: Optional[List[str]],
    filter_config: Optional[Dict] = None
) -> int:
    """Process all items in mapping; return exit code."""
    start_time = time.time()

    print()

    _check_tools_or_die()

    cookies_env = None
    if not cookies_from_browser and not cookies:
        cookies_env = os.getenv("COOKIES_TXT") or os.getenv("YT_COOKIES")

    try:
        mapping_obj = json.loads(mapping.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ERROR] Failed to read mapping: {e}")
        return 2
    if not isinstance(mapping_obj, dict):
        print("[ERROR] Mapping must be a JSON object.")
        return 2

    # Apply filters
    if filter_subjects or filter_recordings or filter_stages or filter_config:
        filtered_keys = [k for k in mapping_obj.keys() if _should_process_key(k, filter_subjects, filter_recordings, filter_stages, filter_config)]
        
        # Print filter info
        if filter_config:
            print(f"[INFO] Using filter config with {len(filter_config)} subjects")
        else:
            subjects_str = ','.join(filter_subjects) if filter_subjects else 'all'
            recordings_str = ','.join(filter_recordings) if filter_recordings else 'all'
            stages_str = ','.join(filter_stages) if filter_stages else 'all'
            
            print(f"[INFO] Applied filters:")
            print(f"  Subjects:   {subjects_str}")
            print(f"  Recordings: {recordings_str}")
            print(f"  Stages:     {stages_str}")
        print(f"[INFO] Filtered {len(mapping_obj)} items down to {len(filtered_keys)} items.")
        
        keys = sorted(filtered_keys)
    else:
        keys = sorted(mapping_obj.keys())
    
    total = len(keys)
    if total == 0:
        print("[INFO] No items found in mapping.")
        elapsed = time.time() - start_time
        hrs, rem = divmod(int(elapsed), 3600)
        mins, secs = divmod(rem, 60)
        print("\n[SUMMARY]")
        print("  Downloaded: 0")
        print("  Skipped:    0")
        print("  Failed:     0")
        print()
        print(f"[INFO] Total runtime: {hrs}h {mins}m {secs}s – script: 'download_recordings_from_metadata.py' completed successfully!")
        print()
        return 0

    print(f"[INFO] Found {total} item(s) in mapping.")

    downloaded = skipped = failed = 0
    failed_items: dict[str, str] = {}
    downloaded_paths = []

    for i, key in enumerate(keys, 1):
        url = _mapping_value_to_url(mapping_obj[key])
        if not url:
            print(f"\n[{i}/{total}] {key}")
            print("  -> Skipping (no URL).")
            skipped += 1
            continue

        print(f"\n[{i}/{total}] {key}")

        ok, status, elapsed = _process_one(
            key, url, out,
            cookies or cookies_env, cookies_from_browser,
            force, user_agent, insecure_ssl, rate_limit, retries
        )

        if ok:
            if status == "existing":
                print("  -> Skipping (exists).")
                skipped += 1
            else:
                hrs, rem = divmod(int(elapsed), 3600)
                mins, secs = divmod(rem, 60)
                if hrs:
                    time_str = f"{hrs}h {mins:02d}m {secs:02d}s"
                elif mins:
                    time_str = f"{mins}m {secs:02d}s"
                else:
                    time_str = f"{secs}s"
                print(f"  -> Downloaded ({time_str})")
                downloaded += 1
                
                # Track downloaded file path
                try:
                    _, _, _, recording_id, rel = _parse_key_path(key)
                    rec_n = _recording_number(recording_id)
                    dest_dir = out / rel
                    target = dest_dir / f"recording_{rec_n}.wav"
                    downloaded_paths.append(str(target))
                except Exception:
                    pass
        else:
            print(f"  -> FAILED: {status}")
            failed += 1
            failed_items[key] = url

    elapsed = time.time() - start_time
    hrs, rem = divmod(int(elapsed), 3600)
    mins, secs = divmod(rem, 60)

    print("\n[SUMMARY]")
    print(f"  Downloaded: {downloaded}")
    print(f"  Skipped:    {skipped}")
    print(f"  Failed:     {failed}")
    if downloaded_paths:
        print(f"  Output files:")
        for path in downloaded_paths:
            print(f"    - {path}")

    if failed_items:
        results_dir.mkdir(parents=True, exist_ok=True)
        retry_path = results_dir / "retry_mapping.json"
        retry_path.write_text(json.dumps(failed_items, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n[INFO] Retry mapping written to: {retry_path}")

    print()
    print(f"[INFO] Total runtime: {hrs}h {mins}m {secs}s – script: 'download_recordings_from_metadata.py' completed successfully!")
    print()

    return 0 if failed == 0 else 1


# ────────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download recordings from a mapping JSON and write one canonical WAV per item."
    )
    parser.add_argument("--mapping", required=True, type=Path,
                        help="Path to mapping JSON file.")
    parser.add_argument("--out", required=True, type=Path,
                        help="Output root directory for audio files.")
    parser.add_argument("--cookies", type=str, default=None,
                        help="Optional path to cookies.txt file.")
    parser.add_argument("--cookies-from-browser", type=str, default=None,
                        help="Optional browser to extract cookies from.")
    parser.add_argument("--force", action="store_true",
                        help="Force re-download of existing files.")
    parser.add_argument("--user-agent", type=str,
                        default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        help="Custom user agent string.")
    parser.add_argument("--insecure-ssl", action="store_true",
                        help="Skip SSL certificate verification.")
    parser.add_argument("--rate-limit", type=str, default=None,
                        help="Download rate limit (e.g., '1M').")
    parser.add_argument("--retries", type=int, default=5,
                        help="Number of download retries (default: 5).")
    parser.add_argument("--results-dir", type=Path, default=Path("./results"),
                        help="Directory for run summaries (default: ./results).")
    parser.add_argument("--filter-subjects", type=str, default=None,
                        help="Comma-separated subject IDs (e.g., 'pd_01,pd_02,control_05').")
    parser.add_argument("--filter-recordings", type=str, default=None,
                        help="Comma-separated recording IDs (e.g., 'recording_1,recording_3').")
    parser.add_argument("--filter-stages", type=str, default=None,
                        help="Comma-separated stages (e.g., 'Pre-Diagnosis,Post-Diagnosis').")
    parser.add_argument("--filter-config", type=Path, default=None,
                        help="Path to JSON config file for advanced filtering.")
    args = parser.parse_args()

    # Parse comma-separated values
    filter_subjects = [s.strip() for s in args.filter_subjects.split(',')] if args.filter_subjects else None
    filter_recordings = [r.strip() for r in args.filter_recordings.split(',')] if args.filter_recordings else None
    filter_stages = [st.strip() for st in args.filter_stages.split(',')] if args.filter_stages else None

    # Load filter config if provided
    filter_config = None
    if args.filter_config:
        try:
            filter_config = _load_filter_config(args.filter_config)
            print(f"[INFO] Loaded filter config from: {args.filter_config}")
        except Exception as e:
            print(f"[ERROR] Failed to load filter config: {e}")
            raise SystemExit(2)

    try:
        code = run(
            args.mapping, args.out, args.cookies, args.cookies_from_browser,
            args.force, args.user_agent, args.insecure_ssl, args.rate_limit,
            args.retries, args.results_dir, filter_subjects, filter_recordings,
            filter_stages, filter_config
        )
        raise SystemExit(code)
    except KeyboardInterrupt:
        print("\n\n[INFO] Download interrupted by user (Ctrl+C).")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
