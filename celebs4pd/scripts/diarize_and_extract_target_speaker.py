#!/usr/bin/env python3
# diarize_and_extract_target_speaker.py
# Author: Nadine El-Mufti (2025)

"""
Purpose
-------
Run speaker diarization on each full-length recording WAV to identify individual speakers,
then extract only the target speaker's voice based on the "Speaker ID" field in metadata.

⚠️  WARNING: This operation modifies your dataset structure:
  - Original recordings are moved to "Non-Diarized Recording" subdirectory
  - Main recording files are replaced with extracted single-speaker audio
  - Process creates .diarized marker files to track completion
  - Use --force to reprocess already-completed recordings

Inputs
------
--dataset-root       : Root of audio, e.g. "./Celebs4PD Dataset"
--metadata-root      : Root of metadata, e.g. "./Celebs4PD Metadata"
--hf-token           : Hugging Face token for pyannote/speaker-diarization-3.1 (requires authentication)
--num-speakers       : Optional fixed number of speakers (overrides min/max)
--min-speakers       : Optional minimum number of speakers (default: 1)
--max-speakers       : Optional maximum number of speakers (default: 20)
--force              : Force re-processing of already processed files
--filter-subjects    : Comma-separated subject IDs (e.g., "pd_01,pd_02,control_05")
--filter-recordings  : Comma-separated recording IDs (e.g., "recording_1,recording_3")
--filter-stages      : Comma-separated stages (e.g., "Pre-Diagnosis,Post-Diagnosis")
--filter-config      : Path to JSON config file for advanced filtering

Output
------
For each recording (e.g., recording_3.wav):
  - Original moved to: Non-Diarized Recording/recording_3.wav (preserved if not already exists)
  - Extracted single-speaker audio saved as: recording_3.wav (replaces original location)
  - Marker file created: recording_3.wav.diarized (tracks completion status)

Recordings where the target speaker is not detected are skipped with a warning message.

Troubleshooting
---------------
- If authentication fails: Verify HF token has access to pyannote gated models at huggingface.co
- If target speaker not found: Check that "Speaker ID" in metadata matches diarization output labels (e.g., "SPEAKER_00")
- If processing fails partway: Rerun script - already-processed files (with .diarized markers) are automatically skipped
- If you need to reprocess: Use --force flag to override .diarized markers

Example
-------
# Diarize all recordings:
celebs4pd-diarize \
  --dataset-root "./Celebs4PD Dataset" \
  --metadata-root "./Celebs4PD Metadata" \
  --hf-token "<HF_TOKEN>"

# Advanced filtering with config file:
celebs4pd-diarize \
  --dataset-root "./Celebs4PD Dataset" \
  --metadata-root "./Celebs4PD Metadata" \
  --hf-token "<HF_TOKEN>" \
  --filter-config "./filter_config.json"
"""

import argparse
import json
import os
import random
import time
from pathlib import Path
from typing import Optional, Iterable, List, Tuple, Dict
import numpy as np
import soundfile as sf
import torch
import torchaudio
from pyannote.audio import Pipeline
from pyannote.audio.pipelines.utils.hook import ProgressHook

# Set random seeds for deterministic behavior
torch.manual_seed(42)
np.random.seed(42)
random.seed(42)
if torch.cuda.is_available():
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


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

def _should_process_wav(
    wav_path: Path, 
    dataset_root: Path, 
    filter_subjects: Optional[List[str]], 
    filter_recordings: Optional[List[str]], 
    filter_stages: Optional[List[str]],
    filter_config: Optional[Dict] = None
) -> bool:
    """Check if this WAV should be processed based on filters."""
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
    import re
    if not re.match(r"^recording_\d+\.wav$", name):
        return False
    parent_name = path.parent.name
    if re.match(r"^recording_\d+$", parent_name) or parent_name == "Non-Diarized Recording":
        return False
    return True


def _find_recording_wavs(dataset_root: Path) -> Iterable[Path]:
    """Yield eligible top-level full recordings under dataset_root."""
    yield from (p for p in dataset_root.rglob("recording_*.wav") if _is_full_recording_wav(p))


def _recording_id_from_name(path: Path) -> Optional[str]:
    """Return 'recording_N' from filename or None."""
    import re
    stem = path.stem
    return stem if re.match(r"^recording_\d+$", stem) else None


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
    rec_id = _recording_id_from_name(Path(rec_file))
    if not rec_id:
        raise ValueError(f"Bad recording file name: {rec_file}")
    return group, subject, stage, rec_id


def _find_recording_metadata_json(
    recording_wav: Path,
    dataset_root: Path,
    metadata_root: Path
) -> Optional[Path]:
    """
    Find the recording-level metadata JSON under metadata_root mirroring the audio layout.

    Looks for:
      <meta_root>/<Group>/<Subject>/[<Stage>/]<recording_N>/*.json

    Returns the first matching JSON file if found, else None.
    """
    try:
        group, subject, stage, rec_id = _rel_components(recording_wav, dataset_root)
    except Exception:
        return None

    if stage:
        meta_dir = metadata_root / group / subject / stage / rec_id
    else:
        meta_dir = metadata_root / group / subject / rec_id

    if not meta_dir.is_dir():
        return None

    candidates = sorted(meta_dir.glob(f"*{rec_id}*metadata*.json")) or sorted(meta_dir.glob("*.json"))
    for p in candidates:
        if p.is_file():
            return p
    return None


def _load_speaker_id(metadata_json: Path) -> Optional[str]:
    """Load 'Speaker ID' from metadata JSON."""
    try:
        obj = json.loads(metadata_json.read_text(encoding="utf-8"))
    except Exception:
        return None
    val = obj.get("Speaker ID")
    return val.strip() if isinstance(val, str) and val.strip() else None


def _get_marker_path(wav_path: Path) -> Path:
    """Return path to the .diarized marker file for this recording."""
    return wav_path.with_suffix(".wav.diarized")


def _get_original_path(wav_path: Path) -> Path:
    """Return path where the original file should be backed up in 'Non-Diarized Recording' subdirectory."""
    original_dir = wav_path.parent / "Non-Diarized Recording"
    return original_dir / wav_path.name


def _is_already_processed(wav_path: Path) -> bool:
    """Check if this recording has already been processed."""
    return _get_marker_path(wav_path).exists()


def _mark_as_processed(wav_path: Path) -> None:
    """Create a marker file indicating this recording has been processed."""
    _get_marker_path(wav_path).touch()


def _load_pipeline(hf_token: str) -> Pipeline:
    """Load diarization pipeline compatible with pyannote.audio 3.x."""
    if torch.cuda.is_available():
        device = torch.device("cuda")
        print(f"[INFO] Using NVIDIA GPU (CUDA) for diarization")
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device = torch.device("mps")
        print(f"[INFO] Using Apple Silicon GPU (MPS) for diarization")
    else:
        device = torch.device("cpu")
        print(f"[WARNING] Using CPU for diarization - slower processing")

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token,
    )
    pipeline.to(device)
    return pipeline


def _to_annotation(result):
    """Convert result to an Annotation if needed."""
    if hasattr(result, "speaker_diarization"):
        return result.speaker_diarization
    return result


def _diarize(pipeline: Pipeline, wav_path: Path, num_speakers, min_speakers, max_speakers):
    """Run diarization with progress hook; return (annotation, sample_rate)."""
    waveform, sample_rate = torchaudio.load(str(wav_path))
    kwargs = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = int(num_speakers)
    else:
        if min_speakers is not None:
            kwargs["min_speakers"] = int(min_speakers)
        if max_speakers is not None:
            kwargs["max_speakers"] = int(max_speakers)
    with ProgressHook() as hook:
        result = pipeline({"waveform": waveform, "sample_rate": sample_rate}, hook=hook, **kwargs)
    annotation = _to_annotation(result)
    return annotation, sample_rate


def _extract_speaker_audio(diarization, speaker_label: str, wav_path: Path, sample_rate: int) -> Optional[np.ndarray]:
    """Concatenate all turns for speaker_label into a mono float32 array (or None)."""
    detected_speakers = list(diarization.labels())
    print(f"  -> Detected speakers: {detected_speakers}")
    print(f"  -> Looking for speaker: '{speaker_label}'")

    audio, sr = sf.read(str(wav_path), always_2d=True, dtype="float32")
    if sr != sample_rate:
        tensor = torch.from_numpy(audio.T)
        resampled = torchaudio.functional.resample(tensor, sr, sample_rate)
        audio = resampled.T.numpy()
        sr = sample_rate

    mono = audio.mean(axis=1)

    chunks = []
    for segment, _, label in diarization.itertracks(yield_label=True):
        if str(label) != speaker_label:
            continue
        start = int(max(0, np.floor(segment.start * sr)))
        end = int(max(start, np.ceil(segment.end * sr)))
        end = min(end, len(mono))
        if end > start:
            chunks.append(mono[start:end])

    if not chunks:
        print(f"  -> No audio found for speaker '{speaker_label}'")
        return None

    print(f"  -> Found {len(chunks)} chunks for speaker '{speaker_label}'")
    return np.concatenate(chunks).astype(np.float32)


def _preserve_original_and_save(original_path: Path, new_data: np.ndarray, sample_rate: int) -> None:
    """
    Preserve the original file by moving it to 'Non-Diarized Recording' subdirectory,
    then save the new processed audio as the main file.
    """
    original_backup = _get_original_path(original_path)
    
    # Create the Non-Diarized Recording directory if it doesn't exist
    original_backup.parent.mkdir(parents=True, exist_ok=True)
    
    # Only backup if the original backup doesn't exist yet
    if not original_backup.exists():
        print(f"  -> Backing up original to: Non-Diarized Recording/{original_backup.name}")
        os.rename(str(original_path), str(original_backup))
    else:
        print(f"  -> Original backup already exists: Non-Diarized Recording/{original_backup.name}")
        # Remove the current file so we can write the new one
        original_path.unlink()
    
    # Write the new processed file
    sf.write(str(original_path), new_data, sample_rate, subtype="PCM_16")
    print(f"  -> Saved extracted speaker to: {original_path.name}")


# ────────────────────────────────────────────────────────────────────────────────
# Core
# ────────────────────────────────────────────────────────────────────────────────

def run(dataset_root: Path, metadata_root: Path, hf_token: str,
        num_speakers: Optional[int] = None,
        min_speakers: Optional[int] = 1,
        max_speakers: Optional[int] = 20,
        force: bool = False,
        filter_subjects: Optional[List[str]] = None,
        filter_recordings: Optional[List[str]] = None,
        filter_stages: Optional[List[str]] = None,
        filter_config: Optional[Dict] = None) -> int:
    """Process all eligible recordings under dataset_root; return exit code."""
    start_time = time.time()

    print()

    root = dataset_root.resolve()
    meta_root = metadata_root.resolve()
    if not root.exists():
        print(f"[ERROR] Dataset root not found: {root}")
        return 2
    if not meta_root.exists():
        print(f"[ERROR] Metadata root not found: {meta_root}")
        return 2

    try:
        pipeline = _load_pipeline(hf_token)
    except Exception as e:
        print(f"[ERROR] Failed to load diarization pipeline: {e}")
        return 3

    all_wavs = list(_find_recording_wavs(root))
    
    # Apply filters
    if filter_subjects or filter_recordings or filter_stages or filter_config:
        wavs = [w for w in all_wavs if _should_process_wav(w, root, filter_subjects, filter_recordings, filter_stages, filter_config)]
        
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
        print("[INFO] No eligible top-level recordings found.")
        elapsed = time.time() - start_time
        hrs, rem = divmod(int(elapsed), 3600)
        mins, secs = divmod(rem, 60)
        print("\n[SUMMARY]")
        print("  Processed: 0")
        print("  Skipped:   0")
        print("  Failed:    0")
        print()
        print(f"[INFO] Total runtime: {hrs}h {mins}m {secs}s – script: 'diarize_and_extract_target_speaker.py' completed successfully!")
        print()
        return 0

    total = len(wavs)
    processed = skipped = failed = 0
    processed_paths = []
    print(f"[INFO] Found {total} recording(s).")
    print(f"[INFO] Using speaker detection range: min={min_speakers}, max={max_speakers}")

    for i, wav_path in enumerate(sorted(wavs), 1):
        print(f"\n[{i}/{total}] {wav_path}")

        if _is_already_processed(wav_path) and not force:
            print("  -> Skipping (already processed).")
            skipped += 1
            continue

        meta_json = _find_recording_metadata_json(wav_path, root, meta_root)
        if not meta_json:
            print("  -> Skipping (recording metadata not found).")
            skipped += 1
            continue

        speaker_id = _load_speaker_id(meta_json)
        if not speaker_id:
            print("  -> Skipping (Speaker ID missing).")
            skipped += 1
            continue

        try:
            # Determine which file to read: original backup if it exists, otherwise current file
            original_backup = _get_original_path(wav_path)
            source_file = original_backup if original_backup.exists() else wav_path
            
            diarization, sr = _diarize(pipeline, source_file, num_speakers, min_speakers, max_speakers)
            extracted = _extract_speaker_audio(diarization, speaker_id, source_file, sr)
            
            if extracted is None or extracted.size == 0:
                print(f"  -> Skipping (target speaker '{speaker_id}' not present).")
                skipped += 1
                continue

            _preserve_original_and_save(wav_path, extracted, sr)
            _mark_as_processed(wav_path)
            processed += 1
            processed_paths.append(str(wav_path))

        except Exception as e:
            print(f"  -> FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    elapsed = time.time() - start_time
    hrs, rem = divmod(int(elapsed), 3600)
    mins, secs = divmod(rem, 60)

    print("\n[SUMMARY]")
    print(f"  Processed: {processed}")
    print(f"  Skipped:   {skipped}")
    print(f"  Failed:    {failed}")
    if processed_paths:
        print(f"  Output files:")
        for path in processed_paths:
            print(f"    - {path}")
    print()
    print(f"[INFO] Total runtime: {hrs}h {mins}m {secs}s – script: 'diarize_and_extract_target_speaker.py' completed successfully!")
    print()

    return 0 if failed == 0 else 4


# ────────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Diarize each top-level recording, extract the target speaker, preserve original."
    )
    parser.add_argument("--dataset-root", required=True, type=Path,
                        help="Path to dataset root (audio).")
    parser.add_argument("--metadata-root", required=True, type=Path,
                        help="Path to metadata root mirroring the audio layout.")
    parser.add_argument("--hf-token", required=True, type=str,
                        help="Hugging Face token for pyannote/speaker-diarization-3.1.")
    parser.add_argument("--num-speakers", type=int, default=None,
                        help="Optional fixed number of speakers (overrides min/max).")
    parser.add_argument("--min-speakers", type=int, default=1,
                        help="Optional minimum number of speakers (default: 1).")
    parser.add_argument("--max-speakers", type=int, default=20,
                        help="Optional maximum number of speakers (default: 20).")
    parser.add_argument("--force", action="store_true",
                        help="Force re-processing of already processed files.")
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

    code = run(args.dataset_root, args.metadata_root, args.hf_token,
               args.num_speakers, args.min_speakers, args.max_speakers, args.force,
               filter_subjects, filter_recordings, filter_stages, filter_config)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
