#!/usr/bin/env python3
# segment_and_annotate_final_segments.py
# Author: Nadine El-Mufti (2025)
# Optimized version with GPU support, batching, parallelization, and multi-subject support

"""
Purpose
-------
Segment each top-level full-length recording (`recording_N.wav`) into overlapping
windows, then immediately annotate each segment with Whisper transcription and 
comprehensive speech metrics. This ensures atomic operation - every segment WAV
gets corresponding metadata.

⚠️  Note: This script creates many new files (multiple segments per recording plus
metadata JSON files). Ensure adequate disk space is available. Segments are permanent
additions to your dataset.

Inputs
------
--dataset-root       : Path to the dataset root directory
--metadata-root      : Path to metadata root directory
--whisper-model      : Whisper model to use (tiny/base/small/medium/large/large-v2/large-v3, default: small)
                       Larger models provide better transcription accuracy but require more memory and processing time
--batch-size         : Number of segments to transcribe in parallel (default: 4, increase for better GPU utilization)
--num-workers        : Number of parallel workers for feature extraction (default: 4)
--filter-subjects    : Comma-separated subject IDs (e.g., "pd_01,pd_02,control_05")
--filter-recordings  : Comma-separated recording IDs (e.g., "recording_1,recording_3")
--filter-stages      : Comma-separated stages (e.g., "Pre-Diagnosis,Post-Diagnosis")
--filter-config      : Path to JSON config file for advanced filtering

Output
------
For each discovered `recording_N.wav`:
  1. Segment WAV files in `<root>/<Group>/<Subject>/recording_N/` or
     `<root>/<Group>/<Subject>/<Stage>/recording_N/`
  2. Corresponding metadata JSON files in metadata root with transcriptions and speech metrics
  3. Updated recording-level metadata with segment references

Segmentation creates overlapping 20-second windows with 5-second overlap (adjustable in code).
Typical recordings produce multiple segments depending on length.

Troubleshooting
---------------
- If Whisper fails: Ensure sufficient memory for the selected model size (large models require significant RAM/VRAM)
- If spaCy errors occur: Install required models with `python -m spacy download en_core_web_sm` and `python -m spacy download fr_core_news_sm`
- If processing is interrupted: Rerun with same or more specific filters to continue from where it stopped
- If GPU OOM: Reduce --batch-size to 1 or 2

Example
-------
# Process all recordings with small Whisper model:
celebs4pd-segment-and-annotate \
  --dataset-root "./Celebs4PD Dataset" \
  --metadata-root "./Celebs4PD Metadata"

# Advanced filtering with config file:
celebs4pd-segment-and-annotate \
  --dataset-root "./Celebs4PD Dataset" \
  --metadata-root "./Celebs4PD Metadata" \
  --filter-config "./filter_config.json"
"""

from __future__ import annotations
import argparse
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import soundfile as sf
from pydub import AudioSegment
from pydub.silence import detect_nonsilent
import pyphen
import spacy
import parselmouth
from parselmouth.praat import call
import textstat
import torch


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

STAGES = ("Pre-Diagnosis", "Post-Diagnosis")

# Module-level syllable counters and spaCy models
_syllable_counter_en = pyphen.Pyphen(lang='en_US')
_syllable_counter_fr = pyphen.Pyphen(lang='fr_FR')
_nlp_en = None
_nlp_fr = None


def _is_top_level_recording(path: Path) -> bool:
    """
    True if file is 'recording_<N>.wav' and not inside a 'recording_<N>' folder.
    """
    if not path.is_file() or path.suffix.lower() != ".wav":
        return False
    if "segment" in path.name.lower():
        return False
    if not re.fullmatch(r"recording_\d+\.wav", path.name):
        return False
    if re.fullmatch(r"recording_\d+", path.parent.name or ""):
        return False
    if "Non-Diarized Recording" in path.parts:
        return False
    return True


def _find_top_level_recordings(dataset_root: Path) -> Iterable[Path]:
    """Yield eligible top-level full recordings under dataset_root."""
    yield from (p for p in dataset_root.rglob("recording_*.wav") if _is_top_level_recording(p))


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
        group, subject, stage, recording_id = _infer_parts_from_path(wav_path)
        
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
        
        if filter_recordings and recording_id not in filter_recordings:
            return False
        
        return True
    except Exception:
        return False


def _infer_parts_from_path(wav: Path) -> Tuple[str, str, Optional[str], str]:
    """
    Returns (group, subject, stage_or_None, recording_id) from dataset path.
    Expected patterns:
      <root>/<Group>/<Subject>/recording_N.wav
      <root>/<Group>/<Subject>/<Stage>/recording_N.wav
    """
    recording_id = wav.stem  # recording_N
    parent = wav.parent
    stage = None
    subject = None
    group = None

    # Case with stage
    if parent.name in STAGES:
        stage = parent.name
        subject = parent.parent.name
        group = parent.parent.parent.name
    else:
        subject = parent.name
        group = parent.parent.name

    return group, subject, stage, recording_id


def _find_recording_metadata(metadata_root: Path, group: str, subject_id: str, 
                             stage: Optional[str], recording_id: str) -> Optional[Path]:
    """Find recording-level metadata JSON."""
    recording_num = recording_id.split("_")[1]
    
    # Build base path
    if stage:
        base_path = metadata_root / group / subject_id / stage / recording_id
        # Pattern: {subject_id}_{stage}_recording_{num}_metadata.json
        stage_part = stage.lower().replace("-", "_")
        json_name = f"{subject_id}_{stage_part}_recording_{recording_num}_metadata.json"
    else:
        base_path = metadata_root / group / subject_id / recording_id
        # Pattern: {subject_id}_recording_{num}_metadata.json
        json_name = f"{subject_id}_recording_{recording_num}_metadata.json"
    
    json_path = base_path / json_name
    return json_path if json_path.exists() else None


def _get_segment_metadata_path(metadata_root: Path, group: str, subject_id: str,
                               stage: Optional[str], recording_id: str, segment_id: str) -> Path:
    """Get the path where segment metadata should be saved."""
    recording_num = recording_id.split("_")[1]
    
    if stage:
        base_path = metadata_root / group / subject_id / stage / recording_id
        stage_part = stage.lower().replace("-", "_")
        json_name = f"{subject_id}_{stage_part}_recording_{recording_num}_{segment_id}_metadata.json"
    else:
        base_path = metadata_root / group / subject_id / recording_id
        json_name = f"{subject_id}_recording_{recording_num}_{segment_id}_metadata.json"
    
    base_path.mkdir(parents=True, exist_ok=True)
    return base_path / json_name


def _compute_segment_wav_path(dataset_root: Path, group: str, subject_id: str,
                              stage: Optional[str], recording_id: str, segment_num: int) -> str:
    """Compute relative path for segment WAV (relative to dataset root)."""
    if stage:
        rel_path = Path(group) / subject_id / stage / recording_id / f"{recording_id}_segment_{segment_num}.wav"
    else:
        rel_path = Path(group) / subject_id / recording_id / f"{recording_id}_segment_{segment_num}.wav"
    return f"./Celebs4PD Dataset/{rel_path}"  


def segment_audio_with_sliding_window(
    audio_file: str,
    segment_length_ms: int = 20000,
    overlap_ms: int = 5000,
    min_silence_len: int = 1000,
    silence_thresh: int = -40,
) -> List[Dict[str, Any]]:
    """
    Segment an audio file into smaller overlapping windows.

    Returns
    -------
    List of dicts:
      {
        "start_time": float (seconds),
        "end_time": float (seconds),
        "duration": float (seconds),
        "segment_id": int  # 0-based for filename compatibility
      }
    """
    audio = AudioSegment.from_file(audio_file)
    audio_length = len(audio)
    processed_segments: List[Dict[str, Any]] = []

    start_time = 0
    segment_id = 0

    while start_time < audio_length:
        end_time = min(start_time + segment_length_ms, audio_length)
        segment = audio[start_time:end_time]

        nonsilent_ranges = detect_nonsilent(
            segment, min_silence_len=min_silence_len, silence_thresh=silence_thresh
        )
        if nonsilent_ranges:
            processed_segments.append(
                {
                    "start_time": start_time / 1000.0,
                    "end_time": end_time / 1000.0,
                    "duration": (end_time - start_time) / 1000.0,
                    "segment_id": segment_id,
                }
            )
            segment_id += 1

        start_time += (segment_length_ms - overlap_ms)

    return processed_segments


def _write_segment_audio(full_wav: Path, segment: Tuple[float, float], out_wav: Path) -> None:
    """Write a segment slice of full_wav to out_wav (mono float32 PCM16)."""
    start_sec, end_sec = segment
    audio, sr = sf.read(str(full_wav), always_2d=True, dtype="float32")
    mono = audio.mean(axis=1)

    start_idx = max(0, int(np.floor(start_sec * sr)))
    end_idx   = min(len(mono), int(np.ceil(end_sec * sr)))
    clip = mono[start_idx:end_idx].astype(np.float32) if end_idx > start_idx else np.zeros(1, dtype=np.float32)

    sf.write(str(out_wav), clip, sr, subtype="PCM_16")


def _batch_transcribe(model, wav_paths: List[Path], language_code: str, device) -> List[dict]:
    """
    Transcribe multiple segments in batch for better GPU utilization.
    Returns list of transcription results.
    """
    results = []
    
    # Process in batches
    for wav_path in wav_paths:
        try:
            result = _sensitive_transcribe(model, wav_path, language_code)
            results.append(result)
        except Exception as e:
            print(f"      WARNING: Transcription failed for {wav_path.name}: {e}")
            results.append(None)
    
    return results


def _sensitive_transcribe(model, wav_path: Path, language_code: str):
    """Run Whisper transcription with sensitive settings."""
    last = None
    for t in (0.0, 0.2):
        last = model.transcribe(
            str(wav_path),
            task="transcribe",
            language=language_code,
            word_timestamps=True,
            condition_on_previous_text=False,
            temperature=t,
            no_speech_threshold=0.30,
            compression_ratio_threshold=None,
            logprob_threshold=None,
            beam_size=5,        
            best_of=5,          
            verbose=False
        )
        if last and last.get("segments") and any((s.get("text") or "").strip() for s in last["segments"]):
            break
    return last


def _segments_from_transcription(tx):
    """Convert Whisper output into segment dicts."""
    rows = []
    for seg in (tx.get('segments') or []):
        row = {
            "start": float(seg.get("start", 0.0)),
            "end": float(seg.get("end", 0.0)),
            "text": (seg.get("text") or "").strip(),
            "avg_logprob": float(seg.get("avg_logprob", float("nan"))),
            "compression_ratio": float(seg.get("compression_ratio", float("nan"))),
            "no_speech_prob": float(seg.get("no_speech_prob", float("nan")))
        }
        words = seg.get("words") or []
        if words:
            row["word_count"] = len(words)
            row["first_word_time"] = float(words[0].get("start", row["start"]))
            row["last_word_time"] = float(words[-1].get("end", row["end"]))
        else:
            row["word_count"] = len((row["text"] or "").split())
            row["first_word_time"] = row["start"]
            row["last_word_time"] = row["end"]
        rows.append(row)
    return rows


def _syllables(word: str, lang: str = 'en') -> int:
    """Count syllables using dictionary-based hyphenation."""
    if not word:
        return 0
    counter = _syllable_counter_en if lang == 'en' else _syllable_counter_fr
    hyphenated = counter.inserted(word.lower())
    return max(1, hyphenated.count('-') + 1)


def _compute_linguistic_features(text: str, lang_code: str) -> dict:
    """Compute linguistic features using spaCy."""
    nlp = _nlp_en if lang_code == 'en' else _nlp_fr
    doc = nlp(text)
    
    if len(doc) == 0:
        return {
            "TTR (Lemma-based)": 0.0,
            "Function Word Ratio": 0.0,
            "Mean Word Length": 0.0,
            "Noun Ratio": 0.0,
            "Verb Ratio": 0.0,
            "Adjective Ratio": 0.0,
            "Mean Dependency Depth": 0.0
        }
    
    # Lemma-based TTR
    lemmas = [token.lemma_.lower() for token in doc if not token.is_punct and not token.is_space]
    ttr = len(set(lemmas)) / len(lemmas) if lemmas else 0.0
    
    # Function word ratio (using POS tags)
    function_pos = {'ADP', 'AUX', 'CCONJ', 'DET', 'PART', 'PRON', 'SCONJ'}
    function_count = sum(1 for token in doc if token.pos_ in function_pos)
    function_ratio = function_count / len(doc) if len(doc) > 0 else 0.0
    
    # Mean word length
    words = [token.text for token in doc if not token.is_punct and not token.is_space]
    mean_word_length = float(np.mean([len(w) for w in words])) if words else 0.0
    
    # POS distribution
    noun_count = sum(1 for token in doc if token.pos_ == 'NOUN')
    verb_count = sum(1 for token in doc if token.pos_ == 'VERB')
    adj_count = sum(1 for token in doc if token.pos_ == 'ADJ')
    
    noun_ratio = noun_count / len(doc) if len(doc) > 0 else 0.0
    verb_ratio = verb_count / len(doc) if len(doc) > 0 else 0.0
    adj_ratio = adj_count / len(doc) if len(doc) > 0 else 0.0
    
    # Dependency depth (syntactic complexity)
    def get_depth(token):
        depth = 0
        while token.head != token:
            depth += 1
            token = token.head
        return depth
    
    depths = [get_depth(token) for token in doc]
    mean_depth = float(np.mean(depths)) if depths else 0.0
    
    return {
        "TTR (Lemma-based)": float(ttr),
        "Function Word Ratio": float(function_ratio),
        "Mean Word Length": float(mean_word_length),
        "Noun Ratio": float(noun_ratio),
        "Verb Ratio": float(verb_ratio),
        "Adjective Ratio": float(adj_ratio),
        "Mean Dependency Depth": float(mean_depth)
    }


def _compute_syllable_features(text: str, lang_code: str, total_elapsed: float) -> dict:
    """Compute syllable-based features using pyphen."""
    tokens = [w for w in re.findall(r"\b[\w']+\b", text.lower()) if w]
    
    if not tokens:
        return {
            "Syllables Total": 0,
            "Syllables/sec": 0.0,
            "Mean Syllables/Word": 0.0
        }
    
    syll_counts = [_syllables(w, lang_code) for w in tokens]
    syll_total = sum(syll_counts)
    syll_per_sec = (syll_total / total_elapsed) if total_elapsed > 0 else 0.0
    mean_syll_per_word = float(np.mean(syll_counts)) if syll_counts else 0.0
    
    return {
        "Syllables Total": int(syll_total),
        "Syllables/sec": float(syll_per_sec),
        "Mean Syllables/Word": float(mean_syll_per_word)
    }


def _compute_acoustic_features(wav_path: Path) -> dict:
    """Compute acoustic features using Parselmouth (Praat)."""
    try:
        snd = parselmouth.Sound(str(wav_path))
        
        # Pitch analysis
        pitch = snd.to_pitch()
        pitch_values = pitch.selected_array['frequency']
        pitch_values = pitch_values[pitch_values > 0]  # Remove unvoiced frames
        
        if len(pitch_values) > 0:
            pitch_mean = float(np.mean(pitch_values))
            pitch_sd = float(np.std(pitch_values))
            pitch_range = float(np.max(pitch_values) - np.min(pitch_values))
        else:
            pitch_mean = pitch_sd = pitch_range = 0.0
        
        # Jitter (local, absolute)
        point_process = call(snd, "To PointProcess (periodic, cc)", 75, 500)
        jitter = call(point_process, "Get jitter (local, absolute)", 0, 0, 0.0001, 0.02, 1.3)
        
        # Shimmer (local)
        shimmer = call([snd, point_process], "Get shimmer (local)", 0, 0, 0.0001, 0.02, 1.3, 1.6)
        
        # Harmonics-to-Noise Ratio
        harmonicity = snd.to_harmonicity()
        hnr = call(harmonicity, "Get mean", 0, 0)
        
        # Intensity analysis
        intensity = snd.to_intensity()
        intensity_values = intensity.values[0]
        intensity_values = intensity_values[intensity_values > 0]
        
        if len(intensity_values) > 0:
            intensity_mean = float(np.mean(intensity_values))
            intensity_sd = float(np.std(intensity_values))
        else:
            intensity_mean = intensity_sd = 0.0
        
        return {
            "Pitch Mean (Hz)": float(pitch_mean),
            "Pitch SD (Hz)": float(pitch_sd),
            "Pitch Range (Hz)": float(pitch_range),
            "Jitter (local)": float(jitter),
            "Shimmer (local)": float(shimmer),
            "HNR (dB)": float(hnr),
            "Intensity Mean (dB)": float(intensity_mean),
            "Intensity SD (dB)": float(intensity_sd)
        }
    except Exception:
        # Return zeros if acoustic analysis fails
        return {
            "Pitch Mean (Hz)": 0.0,
            "Pitch SD (Hz)": 0.0,
            "Pitch Range (Hz)": 0.0,
            "Jitter (local)": 0.0,
            "Shimmer (local)": 0.0,
            "HNR (dB)": 0.0,
            "Intensity Mean (dB)": 0.0,
            "Intensity SD (dB)": 0.0
        }


def _compute_complexity_metrics(text: str) -> dict:
    """Compute text complexity metrics using textstat."""
    if not text or len(text.strip()) == 0:
        return {
            "Flesch Reading Ease": 0.0,
            "Flesch-Kincaid Grade": 0.0,
            "Gunning Fog": 0.0
        }
    
    return {
        "Flesch Reading Ease": float(textstat.flesch_reading_ease(text)),
        "Flesch-Kincaid Grade": float(textstat.flesch_kincaid_grade(text)),
        "Gunning Fog": float(textstat.gunning_fog(text))
    }


def _compute_temporal_features(segment_rows: list[dict]) -> dict:
    """Compute temporal features (pause analysis, speaking rate)."""
    if not segment_rows:
        return {}

    total_words = sum(s.get('word_count', len(s.get('text', '').split())) for s in segment_rows)
    
    pauses = []
    for i in range(len(segment_rows) - 1):
        gap = segment_rows[i + 1]['start'] - segment_rows[i]['end']
        pauses.append(max(0.0, float(gap)))

    total_elapsed = float(segment_rows[-1]['end'] - segment_rows[0]['start'])
    total_pause_time = float(sum(pauses))
    speaking_time = max(0.0, total_elapsed - total_pause_time)

    speaking_rate_wpm = (total_words / total_elapsed * 60.0) if total_elapsed > 0 else 0.0
    articulation_rate_wps = (total_words / speaking_time) if speaking_time > 0 else 0.0

    pauses_250ms = [p for p in pauses if p >= 0.25]
    pauses_500ms = [p for p in pauses if p >= 0.5]
    pause_percentage = (total_pause_time / total_elapsed) if total_elapsed > 0 else 0.0

    seg_durations = [max(1e-6, float(s['end']) - float(s['start'])) for s in segment_rows]
    words_per_seg = [s.get('word_count', len((s.get('text') or '').split())) for s in segment_rows]
    rates_per_seg = [w/d if d > 0 else 0.0 for w, d in zip(words_per_seg, seg_durations)]

    avg_logprobs = [s.get('avg_logprob') for s in segment_rows if s.get('avg_logprob') is not None]
    mean_avg_logprob = float(np.mean(avg_logprobs)) if avg_logprobs else 0.0

    comp_ratios = [s.get('compression_ratio') for s in segment_rows if s.get('compression_ratio') is not None]
    pct_high_comp = (sum(1 for v in comp_ratios if v is not None and v > 3.0) / len(comp_ratios)) if comp_ratios else 0.0
    pct_low_conf = (sum(1 for v in avg_logprobs if v is not None and v < -1.0) / len(avg_logprobs)) if avg_logprobs else 0.0

    if pauses:
        pause_p50 = float(np.percentile(pauses, 50))
        pause_p90 = float(np.percentile(pauses, 90))
        pause_p95 = float(np.percentile(pauses, 95))
        longest_pause = float(max(pauses))
        pause_mean = float(np.mean(pauses))
        pause_cv = (float(np.std(pauses)) / pause_mean) if pause_mean > 0 else None
    else:
        pause_p50 = pause_p90 = pause_p95 = longest_pause = 0.0
        pause_cv = None

    seg_len_p90 = float(np.percentile(seg_durations, 90)) if seg_durations else 0.0
    mean_words_per_segment = float(np.mean(words_per_seg)) if words_per_seg else 0.0
    rate_stdev = float(np.std(rates_per_seg)) if len(rates_per_seg) > 1 else 0.0

    return {
        "Total Words": int(total_words),
        "Speaking Rate (wpm)": float(speaking_rate_wpm),
        "Articulation Rate (wps)": float(articulation_rate_wps),
        "Total Elapsed (s)": float(total_elapsed),
        "Total Pause Time (s)": float(total_pause_time),
        "Pause % Time": float(pause_percentage),
        "Pause Count >=250ms": int(len(pauses_250ms)),
        "Pause Count >=500ms": int(len(pauses_500ms)),
        "Pause Len p50 (s)": float(pause_p50),
        "Pause Len p90 (s)": float(pause_p90),
        "Rate Stdev (words/s)": float(rate_stdev),
        "Longest Pause (s)": float(longest_pause),
        "Pause p95 (s)": float(pause_p95),
        "Pause CV": (None if pause_cv is None else float(pause_cv)),
        "Speech Seg Len p90 (s)": float(seg_len_p90),
        "Mean Words/Segment": float(mean_words_per_segment),
        "Mean avg_logprob": float(mean_avg_logprob),
        "Pct Low-Confidence Segs": float(pct_low_conf),
        "Pct High-Compression Segs": float(pct_high_comp)
    }


def _compute_features_for_segment(wav_path: Path, transcript_text: str, lang_code: str, 
                                  seg_rows: list, out_path: Path) -> dict:
    """
    Compute all features for a single segment (parallelizable).
    Returns dict of all features.
    """
    temporal_features = _compute_temporal_features(seg_rows)
    linguistic_features = _compute_linguistic_features(transcript_text, lang_code)
    syllable_features = _compute_syllable_features(
        transcript_text, lang_code, temporal_features.get("Total Elapsed (s)", 0.0)
    )
    acoustic_features = _compute_acoustic_features(wav_path)
    complexity_features = _compute_complexity_metrics(transcript_text)
    
    # Combine all features
    features = {}
    features.update(temporal_features)
    features.update(linguistic_features)
    features.update(syllable_features)
    features.update(acoustic_features)
    features.update(complexity_features)
    
    return features


# ────────────────────────────────────────────────────────────────────────────────
# Core
# ────────────────────────────────────────────────────────────────────────────────

def _process_one_recording(
    full_wav: Path, 
    dataset_root: Path,
    metadata_root: Path,
    whisper_model,
    batch_size: int,
    num_workers: int
) -> int:
    """
    Segment one recording and annotate all segments with batching and parallelization.
    Returns segments_created.
    """
    import time as timing
    rec_start = timing.time()
    
    # Parse path
    group, subject, stage, recording_id = _infer_parts_from_path(full_wav)
    parent = full_wav.parent
    
    # Create segment directory in dataset
    seg_dir = parent / recording_id
    seg_dir.mkdir(parents=True, exist_ok=True)
    
    # Load recording-level metadata
    rec_metadata_path = _find_recording_metadata(metadata_root, group, subject, stage, recording_id)
    rec_metadata = {}
    if rec_metadata_path:
        try:
            rec_metadata = json.loads(rec_metadata_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    
    # Determine language
    lang_code = "en"
    if "Spoken Language" in rec_metadata:
        val = (rec_metadata.get("Spoken Language") or "").strip().lower()
        if val.startswith("fr"):
            lang_code = "fr"
    
    # Generate segments
    print(f"  -> Segmenting audio...")
    seg_start = timing.time()
    segments = segment_audio_with_sliding_window(str(full_wav))
    print(f"     Segmentation: {timing.time() - seg_start:.2f}s")
    
    if not segments:
        print(f"  -> WARNING: No segments generated (audio too short or all silence)")
        return 0
    
    print(f"  -> Generated {len(segments)} segments")
    
    # Step 1: Write all segment WAV files
    print(f"  -> Writing segment WAV files...")
    write_start = timing.time()
    segment_wavs = []
    for idx, seg in enumerate(segments, start=1):
        out_wav = seg_dir / f"{recording_id}_segment_{idx}.wav"
        _write_segment_audio(full_wav, (seg["start_time"], seg["end_time"]), out_wav)
        segment_wavs.append(out_wav)
    print(f"     WAV writing: {timing.time() - write_start:.2f}s")
    
    # Step 2: Batch transcription (GPU-optimized)
    print(f"  -> Transcribing segments (batch_size={batch_size})...")
    transcribe_start = timing.time()
    all_transcriptions = []
    
    # Process in batches for better GPU utilization
    for i in range(0, len(segment_wavs), batch_size):
        batch = segment_wavs[i:i+batch_size]
        batch_results = _batch_transcribe(whisper_model, batch, lang_code, None)
        all_transcriptions.extend(batch_results)
    
    print(f"     Transcription: {timing.time() - transcribe_start:.2f}s")
    
    # Step 3: Parallel feature extraction
    print(f"  -> Computing features (workers={num_workers})...")
    features_start = timing.time()
    
    created = 0
    segment_paths = []
    
    # Prepare data for parallel processing
    feature_tasks = []
    for idx, (seg, wav_path, tx) in enumerate(zip(segments, segment_wavs, all_transcriptions), start=1):
        seg_rows = _segments_from_transcription(tx) if tx else []
        transcript_text = " ".join(s.get("text", "").strip() for s in seg_rows if s.get("text")).strip()
        
        feature_tasks.append({
            'idx': idx,
            'wav_path': wav_path,
            'transcript_text': transcript_text,
            'lang_code': lang_code,
            'seg_rows': seg_rows
        })
    
    # Compute features in parallel using ThreadPoolExecutor (I/O bound for file operations)
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        future_to_task = {}
        for task in feature_tasks:
            future = executor.submit(
                _compute_features_for_segment,
                task['wav_path'],
                task['transcript_text'],
                task['lang_code'],
                task['seg_rows'],
                None
            )
            future_to_task[future] = task
        
        # Collect results and write metadata
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            idx = task['idx']
            
            try:
                features = future.result()
                
                # Build paths
                seg_id = f"segment_{idx}"
                seg_metadata_path = _get_segment_metadata_path(
                    metadata_root, group, subject, stage, recording_id, seg_id
                )
                seg_wav_rel_path = _compute_segment_wav_path(
                    dataset_root, group, subject, stage, recording_id, idx
                )
                
                # Build complete metadata
                complete_metadata = {
                    "Subject ID": subject,
                    "Sex": rec_metadata.get("Sex", "None"),
                    "Group": group,
                    "Year of Diagnosis": rec_metadata.get("Year of Diagnosis", "None"),
                    "Date of Recording": rec_metadata.get("Date of Recording", "None"),
                    "Years from Diagnosis": rec_metadata.get("Years from Diagnosis", "None"),
                    "Spoken Language": rec_metadata.get("Spoken Language", "English"),
                    "Speech Type": rec_metadata.get("Speech Type", "None"),
                    "Source": rec_metadata.get("Source", "None"),
                    "Segment WAV Path": seg_wav_rel_path,
                    "Transcript": task['transcript_text'],
                    "Speaker ID": rec_metadata.get("Speaker ID", "SPEAKER_00"),
                    "Segment ID": seg_id,
                    "Age": rec_metadata.get("Age", "None"),
                    "Has PD Content": False,
                    "Whisper Segments": task['seg_rows']
                }
                
                # Add computed features
                complete_metadata.update(features)
                
                # Write metadata
                seg_metadata_path.write_text(
                    json.dumps(complete_metadata, indent=2, ensure_ascii=False), 
                    encoding="utf-8"
                )
                
                # Track for recording-level update
                rel_path = seg_metadata_path.relative_to(metadata_root)
                segment_paths.append(f"./Celebs4PD Dataset/Metadata/{rel_path}")
                
                created += 1
                
            except Exception as e:
                print(f"     WARNING: Feature computation failed for segment {idx}: {e}")
    
    print(f"     Feature extraction: {timing.time() - features_start:.2f}s")
    
    # Update recording-level metadata with Segments list
    if rec_metadata_path and created > 0:
        rec_metadata["Segments"] = sorted(segment_paths)
        rec_metadata_path.write_text(
            json.dumps(rec_metadata, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    
    print(f"  -> Total recording time: {timing.time() - rec_start:.2f}s")
    
    return created


def run(
    dataset_root: Path, 
    metadata_root: Path, 
    whisper_model_name: str,
    batch_size: int = 4,
    num_workers: int = 4,
    filter_subjects: Optional[List[str]] = None,
    filter_recordings: Optional[List[str]] = None,
    filter_stages: Optional[List[str]] = None,
    filter_config: Optional[Dict] = None
) -> int:
    """Process all eligible recordings; return exit code."""
    global _nlp_en, _nlp_fr
    
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

    print(f"[INFO] Loading Whisper model: {whisper_model_name}")
    try:
        import whisper
        
        # Smart device selection with fallback
        if torch.cuda.is_available():
            device = torch.device("cuda")
            print(f"[INFO] Using NVIDIA GPU (CUDA) - Expected speedup: 10-20x")
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device = torch.device("mps")
            print(f"[INFO] Using Apple Silicon GPU (MPS) - Expected speedup: 5-10x")
        else:
            device = torch.device("cpu")
            print(f"[WARNING] Using CPU - This will be VERY slow! Consider using GPU.")
        
        whisper_model = whisper.load_model(whisper_model_name, device=device)
        
    except Exception as e:
        print(f"[ERROR] Failed to load Whisper model: {e}")
        return 3

    print("[INFO] Loading spaCy models...")
    try:
        _nlp_en = spacy.load("en_core_web_sm")
        _nlp_fr = spacy.load("fr_core_news_sm")
    except Exception as e:
        print(f"[ERROR] Failed to load spaCy models: {e}")
        print("[INFO] Install with: python -m spacy download en_core_web_sm && python -m spacy download fr_core_news_sm")
        return 3

    all_recordings = list(_find_top_level_recordings(root))
    
    # Apply filters
    if filter_subjects or filter_recordings or filter_stages or filter_config:
        recordings = [w for w in all_recordings if _should_process_recording(w, root, filter_subjects, filter_recordings, filter_stages, filter_config)]
        
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
        print(f"[INFO] Filtered {len(all_recordings)} recordings down to {len(recordings)} recordings.")
    else:
        recordings = all_recordings
    
    if not recordings:
        print("[INFO] No eligible top-level recordings found.")
        elapsed = time.time() - start_time
        hrs, rem = divmod(int(elapsed), 3600)
        mins, secs = divmod(rem, 60)
        print("\n[SUMMARY]")
        print("  Segments created: 0")
        print("  Recordings processed: 0")
        print()
        print(f"[INFO] Total runtime: {hrs}h {mins}m {secs}s – script: 'segment_and_annotate_final_segments.py' completed successfully!")
        print()
        return 0

    total = len(recordings)
    print(f"[INFO] Found {total} recording(s).")
    print(f"[INFO] Performance settings: batch_size={batch_size}, workers={num_workers}")

    total_created = 0
    
    for i, wav_path in enumerate(recordings, 1):
        print(f"\n[{i}/{total}] {wav_path}")
        created = _process_one_recording(
            wav_path, root, meta_root, whisper_model, batch_size, num_workers
        )
        if created > 0:
            print(f"  -> Segments: {created} created")
        total_created += created

    elapsed = time.time() - start_time
    hrs, rem = divmod(int(elapsed), 3600)
    mins, secs = divmod(rem, 60)

    print("\n[SUMMARY]")
    print(f"  Segments created: {total_created}")
    print(f"  Recordings processed: {total}")
    print()
    print(f"[INFO] Total runtime: {hrs}h {mins}m {secs}s – script: 'segment_and_annotate_final_segments.py' completed successfully!")
    print()

    return 0


# ────────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Segment recordings and immediately annotate with Whisper transcription and speech metrics (optimized)."
    )
    parser.add_argument("--dataset-root", required=True, type=Path,
                        help="Path to dataset root.")
    parser.add_argument("--metadata-root", required=True, type=Path,
                        help="Path to metadata root.")
    parser.add_argument("--whisper-model", type=str, default="small",
                        choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3"],
                        help="Whisper model to use (default: small).")
    parser.add_argument("--batch-size", type=int, default=4,
                        help="Batch size for transcription (default: 4, increase for better GPU utilization).")
    parser.add_argument("--num-workers", type=int, default=4,
                        help="Number of parallel workers for feature extraction (default: 4).")
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
            args.dataset_root, args.metadata_root, args.whisper_model,
            args.batch_size, args.num_workers,
            filter_subjects, filter_recordings, filter_stages, filter_config
        )
        raise SystemExit(code)
    except KeyboardInterrupt:
        print("\n\n[INFO] Processing interrupted by user (Ctrl+C).")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
