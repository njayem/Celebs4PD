#!/usr/bin/env python3
# export_statistics_to_csv.py
# Author: Nadine El-Mufti (2025)

"""
Purpose
-------
Export dataset statistics to CSV files for analysis and visualization.
Collects participant demographics from metadata and recording counts/durations 
from audio files.

Inputs
------
--metadata-root     : Path to metadata directory (required, for participant demographics)
--dataset-root      : Path to dataset directory (optional, for recording counts and durations)
--demographics      : Path to demographics JSON file (optional, for additional demographic data)
--output-dir        : Directory to save CSV files (default: ./stats_csv)

Output
------
CSV files in the output directory:
  - overall_statistics.csv: Total participants, recordings, and hours
  - by_diagnostic_group.csv: Statistics by PD vs Control
  - by_sex.csv: Statistics by sex
  - by_nationality.csv: Statistics by nationality
  - by_race.csv: Statistics by race/ethnicity
  - by_profession.csv: Statistics by profession
  - group_by_sex_crosstab.csv: Crosstab of group by sex

Note: If soundfile is not installed, duration calculations will be skipped.

Example
-------
# Export all statistics:
celebs4pd-stats \
  --metadata-root "./Celebs4PD Metadata" \
  --dataset-root "./Celebs4PD Dataset" \
  --demographics "./demographics.json" \
  --output-dir "./stats"

# Export participant demographics only (without durations):
celebs4pd-stats \
  --metadata-root "./Celebs4PD Metadata" \
  --output-dir "./stats"
"""

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    import soundfile as sf
    HAS_SOUNDFILE = True
except ImportError:
    HAS_SOUNDFILE = False


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

STAGE_DIRS = {"Pre-Diagnosis", "Post-Diagnosis"}


def _parse_group_and_sex(top_dir_name: str) -> Tuple[str, str]:
    """Parse group (PD/Control) and sex from directory name."""
    name = top_dir_name.strip()
    if name.endswith("Control"):
        sex = name.replace("Control", "").strip()
        return "Control", sex
    if name.endswith("with PD"):
        sex = name.replace("with PD", "").strip()
        return "PD", sex
    raise ValueError(f"Unrecognized group directory: {name}")


def _get_audio_duration(wav_path: Path) -> float:
    """Get audio duration in seconds using soundfile."""
    if not HAS_SOUNDFILE:
        return 0.0
    try:
        info = sf.info(str(wav_path))
        return info.duration
    except Exception:
        return 0.0


def _load_demographics(demo_file: Optional[Path]) -> Dict:
    """Load demographics JSON file."""
    if not demo_file or not demo_file.exists():
        return {}
    try:
        return json.loads(demo_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _collect_participant_demographics(metadata_root: Path, demographics: Dict) -> Dict:
    """Collect participant counts and demographics from metadata directory."""
    group_counts = Counter()
    sex_counts = Counter()
    nationality_counts = Counter()
    race_counts = Counter()
    profession_counts = Counter()
    group_sex_counts = Counter()
    subject_to_demo = {}
    
    print(f"[INFO] Collecting participant demographics from: {metadata_root}\n")
    
    for top in metadata_root.iterdir():
        if not top.is_dir():
            continue
        try:
            group, sex = _parse_group_and_sex(top.name)
        except ValueError:
            continue
        
        for subj_dir in top.iterdir():
            if not subj_dir.is_dir():
                continue
            
            subj_id = subj_dir.name
            group_counts[group] += 1
            sex_counts[sex] += 1
            group_sex_counts[(group, sex)] += 1
            
            demo = demographics.get(subj_id, {})
            nationality = demo.get("nationality", "Unknown")
            race = demo.get("race", "Unknown")
            profession = demo.get("profession", "Unknown")
            
            nationality_counts[nationality] += 1
            race_counts[race] += 1
            profession_counts[profession] += 1
            
            subject_to_demo[subj_id] = {
                "group": group,
                "sex": sex,
                "nationality": nationality,
                "race": race,
                "profession": profession
            }
    
    total_participants = sum(group_counts.values())
    print(f"  -> Found {total_participants} participants")
    print(f"     PD: {group_counts.get('PD', 0)}, Control: {group_counts.get('Control', 0)}\n")
    
    return {
        "participant_counts": {
            "group": group_counts,
            "sex": sex_counts,
            "nationality": nationality_counts,
            "race": race_counts,
            "profession": profession_counts,
        },
        "group_sex_counts": group_sex_counts,
        "subject_to_demo": subject_to_demo
    }


def _collect_recording_statistics(dataset_root: Optional[Path], subject_to_demo: Dict) -> Dict:
    """Collect recording counts and durations from dataset directory."""
    recordings_by_group = defaultdict(int)
    recordings_by_sex = defaultdict(int)
    recordings_by_nationality = defaultdict(int)
    recordings_by_race = defaultdict(int)
    recordings_by_profession = defaultdict(int)
    
    duration_by_group = defaultdict(float)
    duration_by_sex = defaultdict(float)
    duration_by_nationality = defaultdict(float)
    duration_by_race = defaultdict(float)
    duration_by_profession = defaultdict(float)
    
    n_recordings = 0
    total_duration = 0.0
    
    if not dataset_root or not dataset_root.exists():
        print(f"[WARN] Dataset root not found: {dataset_root}")
        print(f"[WARN] Recording counts and durations will be zero.\n")
        return {
            "n_recordings": 0,
            "total_duration": 0.0,
            "recordings_by_group": recordings_by_group,
            "recordings_by_sex": recordings_by_sex,
            "recordings_by_nationality": recordings_by_nationality,
            "recordings_by_race": recordings_by_race,
            "recordings_by_profession": recordings_by_profession,
            "duration_by_group": duration_by_group,
            "duration_by_sex": duration_by_sex,
            "duration_by_nationality": duration_by_nationality,
            "duration_by_race": duration_by_race,
            "duration_by_profession": duration_by_profession,
        }
    
    if not HAS_SOUNDFILE:
        print(f"[WARN] soundfile library not installed. Cannot calculate durations.")
        print(f"[INFO] Install with: pip install soundfile\n")
    
    print(f"[INFO] Scanning dataset for recordings: {dataset_root}\n")
    
    for group_dir in dataset_root.iterdir():
        if not group_dir.is_dir():
            continue
        
        for subject_dir in group_dir.iterdir():
            if not subject_dir.is_dir():
                continue
            
            subject_id = subject_dir.name
            demo = subject_to_demo.get(subject_id, {})
            
            if not demo:
                print(f"[WARN] No demographics found for subject: {subject_id}")
                continue
            
            group = demo.get("group", "Unknown")
            sex = demo.get("sex", "Unknown")
            nationality = demo.get("nationality", "Unknown")
            race = demo.get("race", "Unknown")
            profession = demo.get("profession", "Unknown")
            
            wav_files = []
            
            stage_dirs = [d for d in subject_dir.iterdir() 
                         if d.is_dir() and d.name in STAGE_DIRS]
            
            if stage_dirs:
                for stage_dir in stage_dirs:
                    wav_files.extend(list(stage_dir.glob("recording_*.wav")))
            else:
                wav_files.extend(list(subject_dir.glob("recording_*.wav")))
            
            if wav_files:
                print(f"  -> {subject_id}: {len(wav_files)} recordings")
            
            for wav_file in wav_files:
                n_recordings += 1
                recordings_by_group[group] += 1
                recordings_by_sex[sex] += 1
                recordings_by_nationality[nationality] += 1
                recordings_by_race[race] += 1
                recordings_by_profession[profession] += 1
                
                duration = _get_audio_duration(wav_file)
                total_duration += duration
                duration_by_group[group] += duration
                duration_by_sex[sex] += duration
                duration_by_nationality[nationality] += duration
                duration_by_race[race] += duration
                duration_by_profession[profession] += duration
    
    print(f"\n  -> Total recordings: {n_recordings}")
    print(f"  -> Total duration: {total_duration / 3600.0:.2f} hours\n")
    
    return {
        "n_recordings": n_recordings,
        "total_duration": total_duration,
        "recordings_by_group": recordings_by_group,
        "recordings_by_sex": recordings_by_sex,
        "recordings_by_nationality": recordings_by_nationality,
        "recordings_by_race": recordings_by_race,
        "recordings_by_profession": recordings_by_profession,
        "duration_by_group": duration_by_group,
        "duration_by_sex": duration_by_sex,
        "duration_by_nationality": duration_by_nationality,
        "duration_by_race": duration_by_race,
        "duration_by_profession": duration_by_profession,
    }


def _export_to_csv(data: Dict, output_dir: Path) -> None:
    """Export statistics to CSV files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    with open(output_dir / "overall_statistics.csv", 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Metric", "Value"])
        writer.writerow(["Total Participants", data['n_participants']])
        writer.writerow(["Total Recordings", data['n_recordings']])
        writer.writerow(["Total Hours", f"{data['total_hours']:.2f}"])
    print(f"  -> Saved: overall_statistics.csv")
    
    with open(output_dir / "by_diagnostic_group.csv", 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Group", "Participants", "Recordings", "Duration (hours)", "Avg Recording Length (min)"])
        for group in ["PD", "Control"]:
            participants = data['group_counts'].get(group, 0)
            recordings = data['recordings_by_group'].get(group, 0)
            hours = data['duration_by_group'].get(group, 0.0) / 3600.0
            avg_length = (hours * 60 / recordings) if recordings > 0 else 0
            writer.writerow([group, participants, recordings, f"{hours:.2f}", f"{avg_length:.2f}"])
    print(f"  -> Saved: by_diagnostic_group.csv")
    
    with open(output_dir / "by_sex.csv", 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Sex", "Participants", "Recordings", "Duration (hours)", "Avg Recording Length (min)"])
        for sex in ["Female", "Male"]:
            participants = data['sex_counts'].get(sex, 0)
            recordings = data['recordings_by_sex'].get(sex, 0)
            hours = data['duration_by_sex'].get(sex, 0.0) / 3600.0
            avg_length = (hours * 60 / recordings) if recordings > 0 else 0
            writer.writerow([sex, participants, recordings, f"{hours:.2f}", f"{avg_length:.2f}"])
    print(f"  -> Saved: by_sex.csv")
    
    with open(output_dir / "by_nationality.csv", 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Nationality", "Participants", "Recordings", "Duration (hours)", "Avg Recording Length (min)", "Percentage"])
        total = sum(data['nationality_counts'].values())
        for nat, count in data['nationality_counts'].most_common():
            recordings = data['recordings_by_nationality'].get(nat, 0)
            hours = data['duration_by_nationality'].get(nat, 0.0) / 3600.0
            avg_length = (hours * 60 / recordings) if recordings > 0 else 0
            pct = (count / total) * 100 if total > 0 else 0
            writer.writerow([nat, count, recordings, f"{hours:.2f}", f"{avg_length:.2f}", f"{pct:.1f}%"])
    print(f"  -> Saved: by_nationality.csv")
    
    with open(output_dir / "by_race.csv", 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Race/Ethnicity", "Participants", "Recordings", "Duration (hours)", "Avg Recording Length (min)", "Percentage"])
        total = sum(data['race_counts'].values())
        for race, count in data['race_counts'].most_common():
            recordings = data['recordings_by_race'].get(race, 0)
            hours = data['duration_by_race'].get(race, 0.0) / 3600.0
            avg_length = (hours * 60 / recordings) if recordings > 0 else 0
            pct = (count / total) * 100 if total > 0 else 0
            writer.writerow([race, count, recordings, f"{hours:.2f}", f"{avg_length:.2f}", f"{pct:.1f}%"])
    print(f"  -> Saved: by_race.csv")
    
    with open(output_dir / "by_profession.csv", 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Profession", "Participants", "Recordings", "Duration (hours)", "Avg Recording Length (min)", "Percentage"])
        total = sum(data['profession_counts'].values())
        for prof, count in data['profession_counts'].most_common():
            recordings = data['recordings_by_profession'].get(prof, 0)
            hours = data['duration_by_profession'].get(prof, 0.0) / 3600.0
            avg_length = (hours * 60 / recordings) if recordings > 0 else 0
            pct = (count / total) * 100 if total > 0 else 0
            writer.writerow([prof, count, recordings, f"{hours:.2f}", f"{avg_length:.2f}", f"{pct:.1f}%"])
    print(f"  -> Saved: by_profession.csv")
    
    with open(output_dir / "group_by_sex_crosstab.csv", 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Group", "Female", "Male", "Total"])
        
        group_sex = data['group_sex_counts']
        pd_female = group_sex.get(('PD', 'Female'), 0)
        pd_male = group_sex.get(('PD', 'Male'), 0)
        control_female = group_sex.get(('Control', 'Female'), 0)
        control_male = group_sex.get(('Control', 'Male'), 0)
        
        writer.writerow(["PD", pd_female, pd_male, pd_female + pd_male])
        writer.writerow(["Control", control_female, control_male, control_female + control_male])
        writer.writerow(["Total", pd_female + control_female, pd_male + control_male, 
                         pd_female + pd_male + control_female + control_male])
    print(f"  -> Saved: group_by_sex_crosstab.csv")


# ────────────────────────────────────────────────────────────────────────────
# Core
# ────────────────────────────────────────────────────────────────────────────

def run(dataset_root: Optional[Path], metadata_root: Path, 
        demographics_file: Optional[Path], output_dir: Path) -> int:
    """Export all statistics to CSV files; return exit code."""
    if not metadata_root.exists():
        print(f"[ERROR] Metadata root not found: {metadata_root}")
        return 2
    
    demographics = _load_demographics(demographics_file)
    
    print(f"[INFO] Collecting statistics...\n")
    
    demo_data = _collect_participant_demographics(metadata_root, demographics)
    recording_data = _collect_recording_statistics(dataset_root, demo_data["subject_to_demo"])
    
    combined_data = {
        "n_participants": sum(demo_data["participant_counts"]["group"].values()),
        "n_recordings": recording_data["n_recordings"],
        "total_hours": recording_data["total_duration"] / 3600.0,
        "group_counts": demo_data["participant_counts"]["group"],
        "sex_counts": demo_data["participant_counts"]["sex"],
        "nationality_counts": demo_data["participant_counts"]["nationality"],
        "race_counts": demo_data["participant_counts"]["race"],
        "profession_counts": demo_data["participant_counts"]["profession"],
        "group_sex_counts": demo_data["group_sex_counts"],
        "recordings_by_group": recording_data["recordings_by_group"],
        "recordings_by_sex": recording_data["recordings_by_sex"],
        "recordings_by_nationality": recording_data["recordings_by_nationality"],
        "recordings_by_race": recording_data["recordings_by_race"],
        "recordings_by_profession": recording_data["recordings_by_profession"],
        "duration_by_group": recording_data["duration_by_group"],
        "duration_by_sex": recording_data["duration_by_sex"],
        "duration_by_nationality": recording_data["duration_by_nationality"],
        "duration_by_race": recording_data["duration_by_race"],
        "duration_by_profession": recording_data["duration_by_profession"],
    }
    
    print(f"[INFO] Exporting to CSV files: {output_dir}\n")
    _export_to_csv(combined_data, output_dir)
    
    print(f"\n[SUCCESS] All CSV files exported to: {output_dir}")
    return 0


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Export Celebs4PD dataset statistics to CSV files."
    )
    parser.add_argument("--metadata-root", type=Path, required=True,
                       help="Path to metadata directory.")
    parser.add_argument("--dataset-root", type=Path, default=None,
                       help="Path to dataset directory (for recording durations).")
    parser.add_argument("--demographics", type=Path, default=None,
                       help="Path to demographics JSON file.")
    parser.add_argument("--output-dir", type=Path, default=Path("./stats_csv"),
                       help="Directory to save CSV files (default: ./stats_csv).")
    
    args = parser.parse_args()
    code = run(args.dataset_root, args.metadata_root, args.demographics, args.output_dir)
    raise SystemExit(code)


if __name__ == "__main__":
    main()