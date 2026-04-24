# Bonsai AVI Compressor

Utilities for rewriting Bonsai-recorded AVI files to MP4 with corrected playback timing, copying the timestamp CSV into `csv.gz`, and verifying the compressed outputs before deleting originals.

## Files

- `main.py`: wrapper entrypoint for the repo. Use this as the top-level interface.
- `batch_fix_video_timing.py`: batch compressor for a folder of timestamped videos and matching CSV files.
- `fix_video_timing.py`: single-video analysis and rewrite helper.
- `verify_compression.py`: verification script that checks compressed outputs against archived originals.

## Requirements

- Python `>=3.11`
- `ffmpeg` and `ffprobe` available on `PATH`
- Python dependency: `pandas>=3.2`

## Setup

From the repo root:

```bash
uv sync
```

This creates `.venv` and installs the Python dependencies from `pyproject.toml` / `uv.lock`.

## Expected Folder Layout

The Windows machine is expected to have the source Bonsai videos and their timestamp CSV files in the repo folder. Compressed outputs are written to `compressed/`, and originals can optionally be archived to `compressed/originals/`.

Typical layout after running the batch compressor:

```text
repo/
  batch_fix_video_timing.py
  fix_video_timing.py
  main.py
  verify_compression.py
  compressed/
    <timestamp>_<prefix>.mp4
    <timestamp>_<prefix>.csv.gz
    originals/
      <prefix>_<timestamp>.avi
      <prefix>_<timestamp>.csv
```

## Main Workflows

### 1. Compress a folder of videos

Use the wrapper with the `compress` command. Everything after `--` is forwarded to `batch_fix_video_timing.py`.

```bash
uv run python main.py compress -- --folder . --output-dir compressed --archive-originals
```

Useful options from the batch script:

- `--fps <value>`: override output FPS for all videos
- `--round-fps`: round CSV-observed FPS to the nearest integer
- `--crf <value>`: FFmpeg CRF setting
- `--preset <value>`: FFmpeg preset
- `--overwrite`: replace existing outputs
- `--dry-run`: analyze only, do not write files

### 2. Compress one video

```bash
uv run python main.py compress-one -- path/to/video.avi --rewrite
```

Everything after `--` is forwarded to `fix_video_timing.py`.

### 3. Verify compressed outputs before deleting originals

```bash
uv run python main.py verify -- --compressed-dir compressed --originals-dir compressed/originals
```

The verifier checks:

- decompressed `csv.gz` content matches the original CSV exactly
- CSV row count and first/last timestamps match
- CSV timestamps parse and remain monotonic
- MP4 duration is close to CSV-derived duration
- MP4 FPS matches expected FPS
- MP4 frame count is close to CSV row count
- compressed video dimensions match the original
- sampled frame bursts from the compressed video remain visually close to the original

Optional stronger corruption checks:

- `--decode-coverage-pct <pct>`: decode evenly spaced chunks from each MP4
- `--full-decode`: decode the entire MP4 stream end-to-end

## Suggested Usage Order

1. Run `compress` to create MP4 and `csv.gz` outputs.
2. Run `verify` against `compressed/` and `compressed/originals/`.
3. Delete archived originals only after verification passes.
