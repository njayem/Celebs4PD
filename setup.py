from setuptools import setup, find_packages

setup(
    name="celebs4pd",
    version="0.1.0",
    author="Nadine El-Mufti",
    description="Celebs4PD dataset processing pipeline",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "numpy",
        "soundfile",
        "torch",
        "torchaudio",
        "pyannote.audio",
        "openai-whisper",
        "spacy",
        "pyphen",
        "praat-parselmouth",
        "textstat",
        "pydub",
        "tqdm",
    ],
    entry_points={
        "console_scripts": [
            "celebs4pd-download-data=celebs4pd.scripts.download_recordings_from_metadata:main",
            "celebs4pd-denoise-and-remove-overlaps=celebs4pd.scripts.denoise_and_remove_overlaps:main",
            "celebs4pd-diarize=celebs4pd.scripts.diarize_and_extract_target_speaker:main",
            "celebs4pd-segment-and-annotate=celebs4pd.scripts.segment_and_annotate_final_segments:main",
            "celebs4pd-stats=celebs4pd.scripts.export_statistics_to_csv:main",
        ],
    },
)
