#!/usr/bin/env python3
# denoise_and_remove_overlaps.py
# Author: Nadine El-Mufti (2025)

"""
Purpose
-------
Clean all full-length recordings by:
  1. Removing background noise/music via Deezer's Spleeter 2-stem separation
     (keeps the **vocals** stem only; accompaniment is discarded).
  2. Removing overlapped speech regions using pyannote Overlapped Speech Detection.

⚠️  WARNING: This operation modifies files IN-PLACE. Original recordings are permanently 
replaced with cleaned versions. Back up your dataset before running if you need to preserve 
the originals.

Note: Processing time depends on recording length and hardware. GPU acceleration is 
recommended for faster processing.

Inputs
------
--dataset-root        : Path to the dataset root directory
--hf-token            : Hugging Face token for pyannote models (required for authentication)
--spleeter-env-name   : Name of the conda/mamba env with Deezer's Spleeter installed (default: 'spleeter')
                        Note: Ensure Spleeter and its dependencies are properly installed in this environment
--filter-subjects     : Comma-separated subject IDs (e.g., "pd_01,pd_02,control_05")
--filter-recordings   : Comma-separated recording IDs (e.g., "recording_1,recording_3")
--filter-stages       : Comma-separated stages (e.g., "Pre-Diagnosis,Post-Diagnosis")
--filter-config       : Path to JSON config file for advanced filtering

Output
------
Cleaned recording WAV files (in-place replacement of originals).
Files maintain the same filename, location, and sample rate as the input.
Temporary files (.tmp.wav, stem folders) are automatically cleaned up after processing.

Troubleshooting
---------------
- If Spleeter fails: Verify the conda environment exists and has spleeter installed
  (`mamba list -n spleeter | grep spleeter`)
- If pyannote fails: Check your HF token has access to gated models at huggingface.co
- If interrupted: Rerun the script with the same or more specific filters to continue processing

Example
-------
# Denoise all recordings (full dataset):
celebs4pd-denoise-and-remove-overlaps \
  --dataset-root "./Celebs4PD Dataset" \
  --hf-token "<HF_TOKEN>"

# Advanced filtering with config file:
celebs4pd-denoise-and-remove-overlaps \
  --dataset-root "./Celebs4PD Dataset" \
  --hf-token "<HF_TOKEN>" \
  --filter-config "./filter_config.json"
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Dict
import numpy as np
import soundfile as sf
import torch
import torchaudio
from pyannote.audio import Pipeline


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

def _is_full_recording_wav(path: Path) -> bool:
    """
    True only if:
      • file is *.wav named recording_<number>.wav
      • parent directory name is NOT 'recording_<number>' (avoid nested segment folders)
      • parent directory name is NOT 'Non-Diarized Recording'
      • name does not include 'segment' or '_original'
    """
    if not path.is_file() or path.suffix.lower() != ".wav":
        return False
    name = path.name
    if "segment" in name.lower() or "_original" in name.lower():
        return False
    if not re.match(r"^recording_\d+\.wav$", name):
        return False
    parent_name = path.parent.name
    if re.match(r"^recording_\d+$", parent_name) or parent_name == "Non-Diarized Recording":
        return False
    return True


def _find_recording_wavs(dataset_root: Path) -> Iterable[Path]:
    """Yield eligible top-level full recordings under dataset_root."""
    yield from (p for p in dataset_root.rglob("recording_*.wav") if _is_full_recording_wav(p))


def _rel_components(wav_path: Path, dataset_root: Path) -> Tuple[str, str, Optional[str], str]:
    """
    Parse (group, subject, stage, recording_id) from the WAV path relative to dataset_root.
    Supports:
      <root>/<Group>/<Subject>/recording_N.wav
      <root>/<Group>/<Subject>/<Stage>/recording_N.wav
    """
    rel = wav_path.resolve().relative_to(dataset_root.resolve())
    parts = list(rel.parts)
    if len(parts) < 3:
        raise ValueError(f"Unexpected audio path layout: {wav_path}")
    group = parts[0]
    subject = parts[1]
    if len(parts) == 3:
        stage = None
        rec_file = parts[2]
    else:
        stage = parts[2]
        rec_file = parts[3]
    rec_match = re.match(r"^(recording_\d+)\.wav$", rec_file)
    if not rec_match:
        raise ValueError(f"Bad recording file name: {rec_file}")
    rec_id = rec_match.group(1)
    return group, subject, stage, rec_id


def _should_process_recording(
    wav_path: Path, 
    dataset_root: Path, 
    filter_subjects: Optional[List[str]], 
    filter_recordings: Optional[List[str]], 
    filter_stages: Optional[List[str]],
    filter_config: Optional[Dict] = None
) -> bool:
    """Check if this recording should be processed based on filters."""
    if not any([filter_subjects, filter_recordings, filter_stages, filter_config]):
        return True
    
    try:
        _, subject, stage, rec_id = _rel_components(wav_path, dataset_root)
        
        # Priority 1: Use filter_config if provided
        if filter_config:
            if subject not in filter_config:
                return False
            allowed_stages = filter_config[subject]
            if allowed_stages is not None and stage not in allowed_stages:
                return False
        else:
            if filter_subjects and subject not in filter_subjects:
                return False
            if filter_stages and stage not in filter_stages:
                return False
        
        if filter_recordings and rec_id not in filter_recordings:
            return False
        
        return True
    except Exception:
        return False


def _run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or "Unknown error")


def _denoise_with_spleeter_cli(wav_path: Path, env_name: str = "spleeter") -> None:
    """
    Use Spleeter CLI via `mamba run -n <env> spleeter separate -p spleeter:2stems`
    to remove background music/noise (keep only the vocals stem).
    Overwrites wav_path with the vocals stem.
    """
    base_name = wav_path.stem
    out_root = wav_path.parent
    stem_dir = out_root / base_name

    if stem_dir.exists():
        shutil.rmtree(stem_dir, ignore_errors=True)
    out_root.mkdir(parents=True, exist_ok=True)

    # Find mamba in PATH (cross-platform)
    mamba_path = shutil.which("mamba")
    if not mamba_path:
        raise RuntimeError("mamba not found in PATH. Ensure miniforge is installed and activated.")
    
    cmd = [
        mamba_path, "run", "-n", env_name,
        "spleeter", "separate",
        "-p", "spleeter:2stems",
        "-o", str(out_root),
        str(wav_path),
    ]
    _run(cmd, cwd=out_root)

    vocals_src = stem_dir / "vocals.wav"
    if not vocals_src.exists():
        raise RuntimeError("Spleeter CLI did not produce vocals.wav")

    tmp = wav_path.with_suffix(".tmp.wav")
    shutil.move(str(vocals_src), str(tmp))
    os.replace(str(tmp), str(wav_path))
    shutil.rmtree(stem_dir, ignore_errors=True)


def _load_osd_pipeline(hf_token: str) -> Pipeline:
    if torch.cuda.is_available():
        device = torch.device("cuda")
        print(f"[INFO] Using NVIDIA GPU (CUDA) for overlap detection")
    elif torch.backends.mps.is_available():
        device = torch.device("mps")
        print(f"[INFO] Using Apple Silicon GPU (MPS) for overlap detection")
    else:
        device = torch.device("cpu")
        print(f"[WARNING] Using CPU for overlap detection - slower processing")
    
    pipeline = Pipeline.from_pretrained("pyannote/overlapped-speech-detection",
                                        use_auth_token=hf_token)
    pipeline.to(device)
    return pipeline


def _remove_overlaps(pipeline: Pipeline, wav_path: Path) -> None:
    """Detect overlaps and keep only non-overlapped audio."""
    waveform, sr = torchaudio.load(str(wav_path))
    result = pipeline({"waveform": waveform, "sample_rate": sr})

    keep_chunks = []
    for (seg, _, label) in result.itertracks(yield_label=True):
        if label == "SPEECH":
            start = int(max(0, np.floor(seg.start * sr)))
            end = int(np.ceil(seg.end * sr))
            keep_chunks.append(waveform[:, start:end])

    if not keep_chunks:
        return  # no change if nothing detected

    cleaned = torch.cat(keep_chunks, dim=1)
    tmp = wav_path.with_suffix(".tmp.wav")
    sf.write(str(tmp), cleaned.squeeze().numpy(), sr, subtype="PCM_16")
    os.replace(str(tmp), str(wav_path))

    # Clean up pyannote checkpoint files
    checkpoint_dir = wav_path.parent / ".pyannote"
    if checkpoint_dir.exists():
        shutil.rmtree(checkpoint_dir, ignore_errors=True)


# ────────────────────────────────────────────────────────────────────────────────
# Core
# ────────────────────────────────────────────────────────────────────────────────

def run(dataset_root: Path, hf_token: str, spleeter_env_name: str = "spleeter",
        filter_subjects: Optional[List[str]] = None,
        filter_recordings: Optional[List[str]] = None,
        filter_stages: Optional[List[str]] = None,
        filter_config: Optional[Dict] = None) -> int:
    """Process all eligible recordings under dataset_root; return exit code."""
    start_time = time.time()

    print()

    root = dataset_root.resolve()
    if not root.exists():
        print(f"[ERROR] Dataset root not found: {root}")
        return 2

    try:
        osd_pipeline = _load_osd_pipeline(hf_token)
    except Exception as e:
        print(f"[ERROR] Failed to load OSD pipeline: {e}")
        return 3

    all_wavs = list(_find_recording_wavs(root))
    
    # Apply filters
    if filter_subjects or filter_recordings or filter_stages or filter_config:
        wavs = [w for w in all_wavs if _should_process_recording(w, root, filter_subjects, filter_recordings, filter_stages, filter_config)]
        
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
        print(f"[INFO] Filtered {len(all_wavs)} recordings down to {len(wavs)} recordings.")
    else:
        wavs = all_wavs
    
    if not wavs:
        print("[INFO] No eligible recordings found.")
        elapsed = time.time() - start_time
        hrs, rem = divmod(int(elapsed), 3600)
        mins, secs = divmod(rem, 60)
        print("\n[SUMMARY]")
        print("  Processed: 0")
        print("  Failed:    0")
        print()
        print(f"[INFO] Total runtime: {hrs}h {mins}m {secs}s – script: 'denoise_and_remove_overlaps.py' completed successfully!")
        print()
        return 0

    total = len(wavs)
    processed = failed = 0
    print(f"[INFO] Found {total} recording(s).")

    for i, wav_path in enumerate(sorted(wavs), 1):
        print(f"\n[{i}/{total}] {wav_path}")
        try:
            _denoise_with_spleeter_cli(wav_path, env_name=spleeter_env_name)
            _remove_overlaps(osd_pipeline, wav_path)
            print("  -> Cleaned successfully")
            processed += 1
        except Exception as e:
            print(f"  -> FAILED: {e}")
            failed += 1

    elapsed = time.time() - start_time
    hrs, rem = divmod(int(elapsed), 3600)
    mins, secs = divmod(rem, 60)

    print("\n[SUMMARY]")
    print(f"  Processed: {processed}")
    print(f"  Failed:    {failed}")
    print()
    print(f"[INFO] Total runtime: {hrs}h {mins}m {secs}s – script: 'denoise_and_remove_overlaps.py' completed successfully!")
    print()

    return 0 if failed == 0 else 4


# ────────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Denoise (vocals-only via Spleeter) and remove overlaps for all full-length recordings."
    )
    parser.add_argument("--dataset-root", required=True, type=Path,
                        help="Path to dataset root.")
    parser.add_argument("--hf-token", required=True, type=str,
                        help="Hugging Face token for pyannote models.")
    parser.add_argument("--spleeter-env-name", type=str, default="spleeter",
                        help="Name of the conda/mamba env that has Spleeter (default: 'spleeter').")
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

    code = run(args.dataset_root, args.hf_token, args.spleeter_env_name,
               filter_subjects, filter_recordings, filter_stages, filter_config)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
