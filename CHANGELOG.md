# 📜 Changelog

All notable changes to the **Celebs4PD** Dataset will be documented in this file.

This project follows **Semantic Versioning (SemVer)**:
**MAJOR.MINOR.PATCH**
- **MAJOR** — incompatible or structural changes  
- **MINOR** — additions or improvements (backward compatible)  
- **PATCH** — small fixes, documentation, or dependency updates  

---

## [v1.0.0] - 2025-01-XX
### 🎉 Initial Structured Release
- First public release of the **Celebs4PD** Dataset, a curated corpus of natural celebrity speech for Parkinson's Disease research  
- PD and control subjects are **demographically balanced and pair-matched** by sex and age  
- Includes **longitudinal recordings** across disease stages  
- Recordings **segmented into ≤20 s clips** with 5 s overlap  
- Fully **reproducible pipeline** for diarization, segmentation, and annotation  
- Over **40 standardized acoustic, temporal, and linguistic metrics** per segment  
- Comprehensive **metadata schema and documentation** for academic use  
- Licensed under **CC BY 4.0** (academic, non-commercial research)  
- **Package structure**: Installable via `pip install -e .` with console scripts for all pipeline steps
- **CLI tools**: `celebs4pd-download`, `celebs4pd-denoise`, `celebs4pd-diarize`, `celebs4pd-segment`, `celebs4pd-stats`, `celebs4pd-validate`

**GitHub Tag:** [`v1.0.0`](https://github.com/njayem/Celebs4PD/releases/tag/v1.0.0)

---