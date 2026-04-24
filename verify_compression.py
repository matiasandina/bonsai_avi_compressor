#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import statistics
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table


VIDEO_RE = (
    r"^(?P<prefix>.+?)_"
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}_\d{2}_\d{2})"
    r"\.(?P<ext>avi|mp4|mov|mkv)$"
)
SUPPORTED_VIDEO_EXTS = {".avi", ".mp4", ".mov", ".mkv"}
console = Console()


@dataclass
class CsvStats:
    path: Path
    row_count: int
    sha256: str
    first_local_raw: Optional[str]
    last_local_raw: Optional[str]
    first_utc_raw: Optional[str]
    last_utc_raw: Optional[str]
    first_local_dt: Optional[datetime]
    last_local_dt: Optional[datetime]
    duration_s: Optional[float]
    observed_fps: Optional[float]
    invalid_row_count: int
    parse_error_count: int
    non_monotonic_count: int
    duplicate_timestamp_count: int


@dataclass
class VideoStats:
    path: Path
    duration_s: Optional[float]
    fps: Optional[float]
    nb_frames: Optional[int]
    width: Optional[int]
    height: Optional[int]
    codec_name: Optional[str]
    pix_fmt: Optional[str]
    has_audio: bool


@dataclass
class SourcePair:
    original_video: Path
    original_csv: Optional[Path]
    compressed_video: Path
    compressed_csv: Path


@dataclass
class CheckMessage:
    severity: str
    code: str
    detail: str


@dataclass
class VerificationResult:
    pair: SourcePair
    messages: list[CheckMessage] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(message.severity == "FAIL" for message in self.messages)

    @property
    def warned(self) -> bool:
        return any(message.severity == "WARN" for message in self.messages)

    @property
    def status(self) -> str:
        if self.failed:
            return "FAIL"
        if self.warned:
            return "WARN"
        return "PASS"

    def add(self, severity: str, code: str, detail: str) -> None:
        self.messages.append(CheckMessage(severity=severity, code=code, detail=detail))


@dataclass
class BurstComparison:
    anchor_frame: int
    compared_frames: int
    mean_mae: float
    max_mae: float


def progress(message: str) -> None:
    console.print(message)


def format_seconds(seconds: float) -> str:
    total_seconds = max(int(round(seconds)), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes:d}m{secs:02d}s"
    return f"{secs:d}s"


def format_eta(
    completed: int,
    total: int,
    elapsed_s: float,
) -> str:
    if completed <= 0 or total <= completed:
        return "eta --"
    avg_s = elapsed_s / completed
    remaining_s = avg_s * (total - completed)
    return f"eta {format_seconds(remaining_s)}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify compressed mp4/csv.gz outputs against archived originals before "
            "deleting source files."
        )
    )
    parser.add_argument(
        "--compressed-dir",
        type=Path,
        default=Path("compressed"),
        help="Directory containing compressed mp4/csv.gz outputs.",
    )
    parser.add_argument(
        "--originals-dir",
        type=Path,
        default=Path("compressed/originals"),
        help="Directory containing archived original videos and CSVs.",
    )
    parser.add_argument(
        "--duration-tolerance-pct",
        type=float,
        default=1.0,
        help="Maximum allowed mp4 duration error vs CSV duration, as a percent.",
    )
    parser.add_argument(
        "--duration-tolerance-s",
        type=float,
        default=2.0,
        help="Minimum allowed mp4 duration error vs CSV duration, in seconds.",
    )
    parser.add_argument(
        "--fps-tolerance",
        type=float,
        default=0.05,
        help="Maximum allowed absolute FPS error.",
    )
    parser.add_argument(
        "--frame-count-tolerance",
        type=int,
        default=1,
        help="Maximum allowed difference between mp4 frame count and CSV row count.",
    )
    parser.add_argument(
        "--expected-fps",
        type=float,
        default=None,
        help="Override expected output FPS for every video.",
    )
    parser.add_argument(
        "--round-observed-fps",
        action="store_true",
        help="Round CSV-observed FPS before comparing to the mp4 FPS.",
    )
    parser.add_argument(
        "--decode-coverage-pct",
        type=float,
        default=0.0,
        help=(
            "Percent of each compressed mp4 to decode in evenly spaced chunks. "
            "Use 0 to disable chunked decode."
        ),
    )
    parser.add_argument(
        "--decode-segments",
        type=int,
        default=12,
        help="Number of evenly spaced decode chunks used for partial decode.",
    )
    parser.add_argument(
        "--burst-anchors",
        type=int,
        default=12,
        help=(
            "Number of evenly spaced frame-burst comparisons between original and "
            "compressed videos. Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--burst-length",
        type=int,
        default=32,
        help=(
            "Number of consecutive frames to compare at each burst anchor."
        ),
    )
    parser.add_argument(
        "--burst-frame-size",
        type=int,
        default=32,
        help=(
            "Frame size used for burst comparison after downscaling to grayscale."
        ),
    )
    parser.add_argument(
        "--burst-mean-mae-threshold",
        type=float,
        default=12.0,
        help=(
            "Maximum allowed mean grayscale MAE within a burst comparison."
        ),
    )
    parser.add_argument(
        "--burst-max-mae-threshold",
        type=float,
        default=24.0,
        help=(
            "Maximum allowed worst-frame grayscale MAE within a burst comparison."
        ),
    )
    parser.add_argument(
        "--full-decode",
        action="store_true",
        help="Decode each full mp4 stream with ffmpeg instead of only partial chunks.",
    )
    parser.add_argument(
        "--strict-size-check",
        action="store_true",
        help="Fail instead of warn when a compressed mp4 is not smaller than its original.",
    )
    return parser.parse_args()


def normalize_path(path: Path) -> Path:
    return path.resolve()


def parse_fraction(rate_str: Optional[str]) -> Optional[float]:
    if not rate_str or rate_str == "0/0":
        return None
    if "/" in rate_str:
        numerator, denominator = rate_str.split("/", maxsplit=1)
        denominator_value = float(denominator)
        if denominator_value == 0:
            return None
        return float(numerator) / denominator_value
    return float(rate_str)


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def run_cmd_bytes(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, check=False, capture_output=True)


def build_prefixed_name(video_path: Path) -> Path:
    import re

    match = re.match(VIDEO_RE, video_path.name, re.IGNORECASE)
    if not match:
        raise ValueError(f"Video name does not match expected pattern: {video_path.name}")
    prefix = match.group("prefix")
    timestamp = match.group("ts")
    ext = match.group("ext")
    return video_path.with_name(f"{timestamp}_{prefix}.{ext.lower()}")


def infer_matching_csv(video_path: Path) -> Optional[Path]:
    csv_path = video_path.with_suffix(".csv")
    csv_gz_path = video_path.with_suffix(".csv.gz")
    if csv_path.exists():
        return csv_path
    if csv_gz_path.exists():
        return csv_gz_path
    return None


def read_decompressed_bytes(path: Path) -> bytes:
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as handle:
            return handle.read()
    return path.read_bytes()


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def parse_timestamp(value: str) -> Optional[datetime]:
    text = value.strip()
    if not text:
        return None

    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")
    if " " in text and "T" not in text:
        candidates.append(text.replace(" ", "T", 1))

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue

    formats = (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    return None


def analyze_csv(path: Path) -> CsvStats:
    data = read_decompressed_bytes(path)
    sha256 = hashlib.sha256(data).hexdigest()
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))

    row_count = 0
    invalid_row_count = 0
    parse_error_count = 0
    non_monotonic_count = 0
    duplicate_timestamp_count = 0
    first_local_raw: Optional[str] = None
    last_local_raw: Optional[str] = None
    first_utc_raw: Optional[str] = None
    last_utc_raw: Optional[str] = None
    first_local_dt: Optional[datetime] = None
    last_local_dt: Optional[datetime] = None
    previous_local_dt: Optional[datetime] = None

    for row in reader:
        if not row:
            continue
        row_count += 1
        if len(row) < 2:
            invalid_row_count += 1
            continue

        local_raw = row[0].strip()
        utc_raw = row[1].strip()

        if first_local_raw is None:
            first_local_raw = local_raw
            first_utc_raw = utc_raw
        last_local_raw = local_raw
        last_utc_raw = utc_raw

        local_dt = parse_timestamp(local_raw)
        if local_dt is None:
            parse_error_count += 1
            continue

        local_dt = normalize_datetime(local_dt)

        if first_local_dt is None:
            first_local_dt = local_dt
        last_local_dt = local_dt

        if previous_local_dt is not None:
            if local_dt < previous_local_dt:
                non_monotonic_count += 1
            elif local_dt == previous_local_dt:
                duplicate_timestamp_count += 1
        previous_local_dt = local_dt

    duration_s: Optional[float] = None
    observed_fps: Optional[float] = None
    if row_count >= 2 and first_local_dt is not None and last_local_dt is not None:
        duration_s = (last_local_dt - first_local_dt).total_seconds()
        if duration_s > 0:
            observed_fps = (row_count - 1) / duration_s

    return CsvStats(
        path=path,
        row_count=row_count,
        sha256=sha256,
        first_local_raw=first_local_raw,
        last_local_raw=last_local_raw,
        first_utc_raw=first_utc_raw,
        last_utc_raw=last_utc_raw,
        first_local_dt=first_local_dt,
        last_local_dt=last_local_dt,
        duration_s=duration_s,
        observed_fps=observed_fps,
        invalid_row_count=invalid_row_count,
        parse_error_count=parse_error_count,
        non_monotonic_count=non_monotonic_count,
        duplicate_timestamp_count=duplicate_timestamp_count,
    )


def probe_video(path: Path) -> VideoStats:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        (
            "stream=index,codec_type,codec_name,pix_fmt,width,height,"
            "r_frame_rate,avg_frame_rate,nb_frames,duration"
        ),
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    result = run_cmd(cmd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe failed for {path}")

    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        raise RuntimeError(f"No video stream found in {path}")

    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    fps = parse_fraction(video_stream.get("avg_frame_rate"))
    if fps is None or fps <= 0:
        fps = parse_fraction(video_stream.get("r_frame_rate"))

    nb_frames_value = video_stream.get("nb_frames")
    nb_frames = None
    if nb_frames_value not in (None, "N/A"):
        nb_frames = int(nb_frames_value)
    else:
        count_cmd = [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "json",
            str(path),
        ]
        count_result = run_cmd(count_cmd)
        if count_result.returncode == 0:
            count_payload = json.loads(count_result.stdout)
            count_streams = count_payload.get("streams", [])
            if count_streams:
                nb_read_frames = count_streams[0].get("nb_read_frames")
                if nb_read_frames not in (None, "N/A"):
                    nb_frames = int(nb_read_frames)

    duration_value = video_stream.get("duration")
    duration_s = None
    if duration_value not in (None, "N/A"):
        duration_s = float(duration_value)
    else:
        format_duration = payload.get("format", {}).get("duration")
        if format_duration not in (None, "N/A"):
            duration_s = float(format_duration)

    return VideoStats(
        path=path,
        duration_s=duration_s,
        fps=fps,
        nb_frames=nb_frames,
        width=video_stream.get("width"),
        height=video_stream.get("height"),
        codec_name=video_stream.get("codec_name"),
        pix_fmt=video_stream.get("pix_fmt"),
        has_audio=has_audio,
    )


def duration_within_tolerance(
    expected_s: float,
    actual_s: float,
    tolerance_pct: float,
    tolerance_s: float,
) -> bool:
    allowed = max(tolerance_s, abs(expected_s) * (tolerance_pct / 100.0))
    return abs(expected_s - actual_s) <= allowed


def compare_float(
    expected: float,
    actual: float,
    tolerance: float,
) -> bool:
    return math.isfinite(expected) and math.isfinite(actual) and abs(expected - actual) <= tolerance


def build_sample_offsets(duration_s: float, count: int) -> list[float]:
    if count <= 0:
        return []
    if duration_s <= 0:
        return [0.0]
    if count == 1:
        return [0.0]

    offsets: list[float] = []
    for index in range(count):
        fraction = index / (count - 1)
        if index == count - 1:
            fraction = 0.99
        offset = max(0.0, min(duration_s * fraction, max(duration_s - 0.001, 0.0)))
        offsets.append(offset)
    return offsets


def safe_probe_offset(duration_s: float, fraction: float) -> float:
    if duration_s <= 0:
        return 0.0
    safety_margin_s = min(max(duration_s * 0.01, 0.1), 1.0)
    max_offset = max(duration_s - safety_margin_s, 0.0)
    return min(max(duration_s * fraction, 0.0), max_offset)


def build_decode_segments(
    duration_s: float,
    coverage_pct: float,
    segment_count: int,
) -> list[tuple[float, float]]:
    if duration_s <= 0 or coverage_pct <= 0 or segment_count <= 0:
        return []

    total_decode_s = duration_s * (coverage_pct / 100.0)
    segment_duration_s = total_decode_s / segment_count
    if segment_duration_s <= 0:
        return []

    max_start = max(duration_s - segment_duration_s, 0.0)
    segments: list[tuple[float, float]] = []
    for index in range(segment_count):
        center_fraction = (index + 0.5) / segment_count
        center_s = duration_s * center_fraction
        start_s = min(max(center_s - (segment_duration_s / 2.0), 0.0), max_start)
        segments.append((start_s, segment_duration_s))
    return segments


def decode_video_segment(
    video_path: Path,
    start_s: float,
    duration_s: float,
) -> tuple[bool, str]:
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{start_s:.6f}",
        "-t",
        f"{duration_s:.6f}",
        "-i",
        str(video_path),
        "-an",
        "-sn",
        "-dn",
        "-f",
        "null",
        "-",
    ]
    result = run_cmd(cmd)
    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or f"ffmpeg failed for segment {start_s:.3f}s + {duration_s:.3f}s"
        )
        return False, detail
    return True, ""


def decode_full_video(video_path: Path) -> tuple[bool, str]:
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(video_path),
        "-f",
        "null",
        "-",
    ]
    result = run_cmd(cmd)
    if result.returncode != 0:
        detail = result.stderr.strip() or "ffmpeg full decode failed"
        return False, detail
    return True, ""


def build_burst_anchor_starts(
    total_frames: int,
    burst_length: int,
    anchor_count: int,
) -> list[int]:
    if total_frames <= 0 or burst_length <= 0 or anchor_count <= 0:
        return []

    max_start = max(total_frames - burst_length, 0)
    if anchor_count == 1:
        return [0]

    starts: list[int] = []
    for index in range(anchor_count):
        fraction = index / max(anchor_count - 1, 1)
        start = int(round(max_start * fraction))
        starts.append(start)

    deduped: list[int] = []
    for start in starts:
        if not deduped or start != deduped[-1]:
            deduped.append(start)
    return deduped


def extract_frame_burst(
    video_path: Path,
    start_frame: int,
    frame_count: int,
    frame_size: int,
    fps: float,
) -> tuple[Optional[list[bytes]], str]:
    if fps <= 0:
        return None, "Invalid FPS for burst extraction"

    seek_s = max(start_frame / fps, 0.0)
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{seek_s:.6f}",
        "-i",
        str(video_path),
        "-an",
        "-sn",
        "-dn",
        "-vf",
        f"scale={frame_size}:{frame_size},format=gray",
        "-frames:v",
        str(frame_count),
        "-f",
        "rawvideo",
        "-",
    ]
    result = run_cmd_bytes(cmd)
    if result.returncode != 0:
        return None, result.stderr.decode("utf-8", errors="replace").strip()

    frame_bytes = frame_size * frame_size
    if frame_bytes <= 0:
        return None, "Invalid frame size"
    if len(result.stdout) % frame_bytes != 0:
        return None, (
            f"Raw burst size {len(result.stdout)} is not divisible by "
            f"frame size {frame_bytes}"
        )

    frames = [
        result.stdout[offset : offset + frame_bytes]
        for offset in range(0, len(result.stdout), frame_bytes)
    ]
    if not frames:
        return None, "No frames returned"
    return frames, ""


def frame_mae(left: bytes, right: bytes) -> float:
    if len(left) != len(right):
        raise ValueError("Frame sizes do not match")
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


def compare_frame_burst(
    original_video: Path,
    compressed_video: Path,
    start_frame: int,
    frame_count: int,
    frame_size: int,
    original_fps: float,
    compressed_fps: float,
) -> tuple[Optional[BurstComparison], str]:
    original_frames, original_error = extract_frame_burst(
        original_video,
        start_frame,
        frame_count,
        frame_size,
        original_fps,
    )
    if original_frames is None:
        return None, f"original burst extraction failed: {original_error}"

    compressed_frames, compressed_error = extract_frame_burst(
        compressed_video,
        start_frame,
        frame_count,
        frame_size,
        compressed_fps,
    )
    if compressed_frames is None:
        return None, f"compressed burst extraction failed: {compressed_error}"

    compared = min(len(original_frames), len(compressed_frames))
    if compared == 0:
        return None, "No comparable frames returned"
    if compared < frame_count:
        return None, (
            f"Expected {frame_count} frames, got original={len(original_frames)}, "
            f"compressed={len(compressed_frames)}"
        )

    maes = [
        frame_mae(original_frames[index], compressed_frames[index])
        for index in range(compared)
    ]
    return BurstComparison(
        anchor_frame=start_frame,
        compared_frames=compared,
        mean_mae=statistics.mean(maes),
        max_mae=max(maes),
    ), ""


def pair_inputs(compressed_dir: Path, originals_dir: Path) -> tuple[list[SourcePair], list[str]]:
    original_index: dict[str, tuple[Path, Optional[Path]]] = {}
    notes: list[str] = []

    for path in sorted(originals_dir.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_VIDEO_EXTS:
            continue
        try:
            compressed_video_name = build_prefixed_name(path).with_suffix(".mp4").name
        except ValueError:
            notes.append(f"Skipping unrecognized original video name: {path.name}")
            continue
        original_index[compressed_video_name] = (path, infer_matching_csv(path))

    pairs: list[SourcePair] = []
    seen: set[str] = set()
    for path in sorted(compressed_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".mp4":
            continue
        if path.parent == originals_dir:
            continue
        compressed_csv = path.with_suffix(".csv.gz")
        original_video, original_csv = original_index.get(path.name, (None, None))
        if original_video is None:
            notes.append(f"No archived original found for compressed video: {path.name}")
            continue
        pairs.append(
            SourcePair(
                original_video=original_video,
                original_csv=original_csv,
                compressed_video=path,
                compressed_csv=compressed_csv,
            )
        )
        seen.add(path.name)

    for compressed_name, (original_video, _original_csv) in sorted(original_index.items()):
        if compressed_name not in seen:
            notes.append(
                "Archived original has no matching compressed mp4: "
                f"{original_video.name} -> {compressed_name}"
            )

    return pairs, notes


def verify_pair(pair: SourcePair, args: argparse.Namespace) -> VerificationResult:
    result = VerificationResult(pair=pair)
    pair_start = time.monotonic()

    if not pair.compressed_video.exists():
        result.add("FAIL", "missing-compressed-video", f"Missing {pair.compressed_video}")
        return result

    if pair.compressed_video.stat().st_size <= 0:
        result.add("FAIL", "empty-compressed-video", f"Empty file: {pair.compressed_video}")

    if not pair.compressed_csv.exists():
        result.add("FAIL", "missing-compressed-csv", f"Missing {pair.compressed_csv}")
        return result

    if pair.compressed_csv.stat().st_size <= 0:
        result.add("FAIL", "empty-compressed-csv", f"Empty file: {pair.compressed_csv}")

    if pair.original_csv is None or not pair.original_csv.exists():
        result.add(
            "FAIL",
            "missing-original-csv",
            f"No original CSV found for {pair.original_video.name}",
        )
        return result

    progress("[dim]Reading original CSV...[/dim]")
    original_csv_stats = analyze_csv(pair.original_csv)
    progress("[dim]Reading compressed CSV...[/dim]")
    compressed_csv_stats = analyze_csv(pair.compressed_csv)

    if original_csv_stats.sha256 != compressed_csv_stats.sha256:
        result.add(
            "FAIL",
            "csv-byte-mismatch",
            "Decompressed CSV content differs between original and compressed copy.",
        )
    else:
        result.add(
            "PASS",
            "csv-byte-match",
            "Decompressed CSV content matches exactly.",
        )

    if original_csv_stats.row_count != compressed_csv_stats.row_count:
        result.add(
            "FAIL",
            "csv-row-count",
            (
                f"Original rows={original_csv_stats.row_count}, "
                f"compressed rows={compressed_csv_stats.row_count}"
            ),
        )
    else:
        result.add(
            "PASS",
            "csv-row-count",
            f"CSV rows match: {original_csv_stats.row_count}",
        )

    if original_csv_stats.first_local_raw != compressed_csv_stats.first_local_raw:
        result.add(
            "FAIL",
            "csv-first-local",
            (
                f"Original first local timestamp={original_csv_stats.first_local_raw!r}, "
                f"compressed={compressed_csv_stats.first_local_raw!r}"
            ),
        )
    if original_csv_stats.last_local_raw != compressed_csv_stats.last_local_raw:
        result.add(
            "FAIL",
            "csv-last-local",
            (
                f"Original last local timestamp={original_csv_stats.last_local_raw!r}, "
                f"compressed={compressed_csv_stats.last_local_raw!r}"
            ),
        )
    if original_csv_stats.first_utc_raw != compressed_csv_stats.first_utc_raw:
        result.add(
            "FAIL",
            "csv-first-utc",
            (
                f"Original first UTC timestamp={original_csv_stats.first_utc_raw!r}, "
                f"compressed={compressed_csv_stats.first_utc_raw!r}"
            ),
        )
    if original_csv_stats.last_utc_raw != compressed_csv_stats.last_utc_raw:
        result.add(
            "FAIL",
            "csv-last-utc",
            (
                f"Original last UTC timestamp={original_csv_stats.last_utc_raw!r}, "
                f"compressed={compressed_csv_stats.last_utc_raw!r}"
            ),
        )

    for label, stats in (
        ("original", original_csv_stats),
        ("compressed", compressed_csv_stats),
    ):
        if stats.invalid_row_count:
            result.add(
                "FAIL",
                f"{label}-csv-invalid-rows",
                f"{label} CSV has {stats.invalid_row_count} malformed row(s).",
            )
        if stats.parse_error_count:
            result.add(
                "FAIL",
                f"{label}-csv-parse-errors",
                f"{label} CSV has {stats.parse_error_count} unparseable timestamp row(s).",
            )
        if stats.non_monotonic_count:
            result.add(
                "FAIL",
                f"{label}-csv-non-monotonic",
                f"{label} CSV has {stats.non_monotonic_count} decreasing timestamp step(s).",
            )
        if stats.duplicate_timestamp_count:
            result.add(
                "WARN",
                f"{label}-csv-duplicate-timestamps",
                f"{label} CSV has {stats.duplicate_timestamp_count} duplicate timestamp step(s).",
            )

    progress("[dim]Probing compressed video...[/dim]")
    compressed_video_stats = probe_video(pair.compressed_video)
    progress("[dim]Probing original video...[/dim]")
    original_video_stats = probe_video(pair.original_video)

    if (
        original_video_stats.width != compressed_video_stats.width
        or original_video_stats.height != compressed_video_stats.height
    ):
        result.add(
            "FAIL",
            "video-dimensions",
            (
                "Original dimensions="
                f"{original_video_stats.width}x{original_video_stats.height}, "
                "compressed dimensions="
                f"{compressed_video_stats.width}x{compressed_video_stats.height}"
            ),
        )
    else:
        result.add(
            "PASS",
            "video-dimensions",
            (
                f"Dimensions preserved at "
                f"{compressed_video_stats.width}x{compressed_video_stats.height}"
            ),
        )

    if compressed_video_stats.has_audio:
        result.add(
            "WARN",
            "compressed-has-audio",
            "Compressed mp4 contains an audio stream.",
        )

    if original_csv_stats.duration_s is None or original_csv_stats.duration_s <= 0:
        result.add(
            "FAIL",
            "csv-duration",
            "Could not derive a positive duration from the original CSV timestamps.",
        )
    elif compressed_video_stats.duration_s is None:
        result.add(
            "FAIL",
            "video-duration",
            "ffprobe did not return a duration for the compressed mp4.",
        )
    else:
        if duration_within_tolerance(
            expected_s=original_csv_stats.duration_s,
            actual_s=compressed_video_stats.duration_s,
            tolerance_pct=args.duration_tolerance_pct,
            tolerance_s=args.duration_tolerance_s,
        ):
            result.add(
                "PASS",
                "video-duration",
                (
                    f"mp4 duration={compressed_video_stats.duration_s:.3f}s, "
                    f"CSV duration={original_csv_stats.duration_s:.3f}s"
                ),
            )
        else:
            result.add(
                "FAIL",
                "video-duration",
                (
                    f"mp4 duration={compressed_video_stats.duration_s:.3f}s, "
                    f"CSV duration={original_csv_stats.duration_s:.3f}s"
                ),
            )

    expected_fps = args.expected_fps
    if expected_fps is None:
        expected_fps = original_csv_stats.observed_fps
        if expected_fps is not None and args.round_observed_fps:
            expected_fps = float(round(expected_fps))

    if expected_fps is None:
        result.add(
            "FAIL",
            "expected-fps",
            "Could not derive expected FPS from CSV timestamps.",
        )
    elif compressed_video_stats.fps is None:
        result.add(
            "FAIL",
            "video-fps",
            "ffprobe did not return FPS for the compressed mp4.",
        )
    else:
        if compare_float(
            expected=expected_fps,
            actual=compressed_video_stats.fps,
            tolerance=args.fps_tolerance,
        ):
            result.add(
                "PASS",
                "video-fps",
                f"mp4 FPS={compressed_video_stats.fps:.6f}, expected={expected_fps:.6f}",
            )
        else:
            result.add(
                "FAIL",
                "video-fps",
                f"mp4 FPS={compressed_video_stats.fps:.6f}, expected={expected_fps:.6f}",
            )

    if compressed_video_stats.nb_frames is None:
        result.add(
            "WARN",
            "video-frame-count-missing",
            "Could not read frame count from the compressed mp4.",
        )
    else:
        frame_delta = abs(compressed_video_stats.nb_frames - original_csv_stats.row_count)
        if frame_delta <= args.frame_count_tolerance:
            result.add(
                "PASS",
                "video-frame-count",
                (
                    f"mp4 frames={compressed_video_stats.nb_frames}, "
                    f"CSV rows={original_csv_stats.row_count}"
                ),
            )
        else:
            result.add(
                "FAIL",
                "video-frame-count",
                (
                    f"mp4 frames={compressed_video_stats.nb_frames}, "
                    f"CSV rows={original_csv_stats.row_count}"
                ),
            )

    original_size = pair.original_video.stat().st_size
    compressed_size = pair.compressed_video.stat().st_size
    if compressed_size >= original_size:
        severity = "FAIL" if args.strict_size_check else "WARN"
        result.add(
            severity,
            "video-size",
            f"Compressed mp4 is not smaller than original ({compressed_size} >= {original_size}).",
        )
    else:
        reduction_pct = 100.0 * (1.0 - (compressed_size / original_size))
        result.add(
            "PASS",
            "video-size",
            f"mp4 size reduced by {reduction_pct:.2f}%",
        )

    if args.decode_coverage_pct > 0:
        if compressed_video_stats.duration_s is None:
            result.add(
                "FAIL",
                "partial-decode",
                "Cannot run partial decode without a known mp4 duration.",
            )
        else:
            segments = build_decode_segments(
                compressed_video_stats.duration_s,
                args.decode_coverage_pct,
                args.decode_segments,
            )
            failures: list[str] = []
            segment_start = time.monotonic()
            total_segments = len(segments)
            with build_loop_progress() as progress_bar:
                task_id = progress_bar.add_task("Partial decode", total=total_segments)
                for segment_index, (start_s, segment_duration_s) in enumerate(
                    segments,
                    start=1,
                ):
                    progress_bar.update(
                        task_id,
                        description=(
                            f"Partial decode {segment_index}/{total_segments} "
                            f"({format_seconds(time.monotonic() - segment_start)})"
                        ),
                    )
                    ok, detail = decode_video_segment(
                        pair.compressed_video,
                        start_s,
                        segment_duration_s,
                    )
                    if not ok:
                        failures.append(
                            f"{start_s:.3f}s + {segment_duration_s:.3f}s: {detail}"
                        )
                    progress_bar.advance(task_id)
            if total_segments:
                progress(
                    "[dim]Partial decode complete "
                    f"in {format_seconds(time.monotonic() - segment_start)}.[/dim]"
                )
            if failures:
                result.add(
                    "FAIL",
                    "partial-decode",
                    "; ".join(failures),
                )
            else:
                result.add(
                    "PASS",
                    "partial-decode",
                    (
                        f"Decoded {args.decode_coverage_pct:.2f}% of the mp4 across "
                        f"{len(segments)} chunk(s)."
                    ),
                )

    if args.burst_anchors > 0 and args.burst_length > 0:
        candidate_counts = [
            count
            for count in (
                original_video_stats.nb_frames,
                compressed_video_stats.nb_frames,
                original_csv_stats.row_count,
            )
            if count is not None and count > 0
        ]
        if not candidate_counts:
            result.add(
                "WARN",
                "content-match",
                "Skipped burst comparison because frame counts are unavailable.",
            )
        else:
            usable_frames = min(candidate_counts)
            frame_size = max(args.burst_frame_size, 1)
            burst_length = min(max(args.burst_length, 1), usable_frames)
            if (
                original_video_stats.fps is None
                or original_video_stats.fps <= 0
                or compressed_video_stats.fps is None
                or compressed_video_stats.fps <= 0
            ):
                result.add(
                    "WARN",
                    "content-match",
                    "Skipped burst comparison because video FPS metadata is unavailable.",
                )
                progress(
                    "[yellow]Burst comparison skipped because one file has no usable FPS metadata.[/yellow]"
                )
                progress(
                    f"[dim]Pair verification complete in {format_seconds(time.monotonic() - pair_start)}.[/dim]"
                )
                return result
            anchor_starts = build_burst_anchor_starts(
                usable_frames,
                burst_length,
                args.burst_anchors,
            )
            burst_failures: list[str] = []
            comparisons: list[BurstComparison] = []
            burst_start = time.monotonic()
            total_anchors = len(anchor_starts)
            with build_loop_progress() as progress_bar:
                task_id = progress_bar.add_task("Burst compare", total=total_anchors)
                for anchor_index, start_frame in enumerate(anchor_starts, start=1):
                    burst_elapsed = time.monotonic() - burst_start
                    progress_bar.update(
                        task_id,
                        description=(
                            f"Burst compare {anchor_index}/{total_anchors} "
                            f"(frame {start_frame}, {format_seconds(burst_elapsed)})"
                        ),
                    )
                    comparison, detail = compare_frame_burst(
                        pair.original_video,
                        pair.compressed_video,
                        start_frame,
                        burst_length,
                        frame_size,
                        original_video_stats.fps,
                        compressed_video_stats.fps,
                    )
                    if comparison is None:
                        burst_failures.append(f"frame {start_frame}: {detail}")
                        progress_bar.advance(task_id)
                        continue
                    comparisons.append(comparison)
                    if comparison.mean_mae > args.burst_mean_mae_threshold:
                        burst_failures.append(
                            (
                                f"frame {start_frame}: burst mean MAE "
                                f"{comparison.mean_mae:.2f} > {args.burst_mean_mae_threshold:.2f}"
                            )
                        )
                    elif comparison.max_mae > args.burst_max_mae_threshold:
                        burst_failures.append(
                            (
                                f"frame {start_frame}: burst max MAE "
                                f"{comparison.max_mae:.2f} > {args.burst_max_mae_threshold:.2f}"
                            )
                        )
                    progress_bar.advance(task_id)
            if total_anchors:
                progress(
                    "[dim]Burst comparison complete "
                    f"in {format_seconds(time.monotonic() - burst_start)}.[/dim]"
                )

            if burst_failures:
                result.add(
                    "FAIL",
                    "content-match",
                    "; ".join(burst_failures),
                )
            elif comparisons:
                overall_mean = statistics.mean(
                    comparison.mean_mae for comparison in comparisons
                )
                overall_max = max(comparison.max_mae for comparison in comparisons)
                result.add(
                    "PASS",
                    "content-match",
                    (
                        f"Burst comparison passed across {len(comparisons)} anchor(s), "
                        f"{burst_length} frames each, overall mean MAE={overall_mean:.2f}, "
                        f"overall max MAE={overall_max:.2f}."
                    ),
                )
            else:
                result.add(
                    "WARN",
                    "content-match",
                    "Skipped burst comparison because no anchors produced comparable frames.",
                )

    if args.full_decode:
        full_decode_start = time.monotonic()
        progress("[dim]Running full decode...[/dim]")
        ok, detail = decode_full_video(pair.compressed_video)
        progress(
            "[dim]Full decode complete "
            f"in {format_seconds(time.monotonic() - full_decode_start)}.[/dim]"
        )
        if ok:
            result.add("PASS", "full-decode", "Full stream decode succeeded.")
        else:
            result.add("FAIL", "full-decode", detail)

    progress(
        f"[dim]Pair verification complete in {format_seconds(time.monotonic() - pair_start)}.[/dim]"
    )
    return result


def print_result(result: VerificationResult) -> None:
    status_styles = {
        "PASS": "green",
        "WARN": "yellow",
        "FAIL": "red",
    }
    details = Table.grid(padding=(0, 1))
    details.add_column(style="bold")
    details.add_column()
    details.add_row("Original video", str(result.pair.original_video))
    details.add_row("Original csv", str(result.pair.original_csv))
    details.add_row("Compressed mp4", str(result.pair.compressed_video))
    details.add_row("Compressed csv", str(result.pair.compressed_csv))

    if result.status == "PASS":
        details.add_row("Result", "[green]PASS all checks[/green]")
    else:
        for message in result.messages:
            if message.severity == "PASS":
                continue
            details.add_row(
                f"[{message.severity}] {message.code}",
                message.detail,
            )

    console.print(
        Panel(
            details,
            title=f"[{status_styles[result.status]}]{result.status}[/{status_styles[result.status]}] "
            f"{result.pair.compressed_video.name}",
            border_style=status_styles[result.status],
        )
    )


def build_loop_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=28),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )


def run_progress_loop(
    total: int,
    description_prefix: str,
    items: list[tuple[int, object]],
    worker,
) -> list[tuple[int, object, object]]:
    results: list[tuple[int, object, object]] = []
    with build_loop_progress() as progress_bar:
        task_id = progress_bar.add_task(description_prefix, total=total)
        for index, payload in items:
            progress_bar.update(
                task_id,
                description=f"{description_prefix} {index}/{total}",
            )
            results.append((index, payload, worker(index, payload)))
            progress_bar.advance(task_id)
    return results


def print_summary(pass_count: int, warn_count: int, fail_count: int) -> None:
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="green")
    summary.add_column(style="yellow")
    summary.add_column(style="red")
    summary.add_row(
        f"PASS: {pass_count}",
        f"WARN: {warn_count}",
        f"FAIL: {fail_count}",
    )
    console.print(Panel(summary, title="Summary", border_style="cyan"))


def print_header(compressed_dir: Path, originals_dir: Path, matched_pairs: int) -> None:
    info = Table.grid(padding=(0, 1))
    info.add_column(style="bold cyan")
    info.add_column()
    info.add_row("Compressed dir", str(compressed_dir))
    info.add_row("Originals dir", str(originals_dir))
    info.add_row("Matched pairs", str(matched_pairs))
    console.print(Panel(info, title="Verification", border_style="cyan"))


def print_pairing_notes(notes: list[str]) -> None:
    body = "\n".join(f"- {note}" for note in notes)
    console.print(Panel(body, title="Pairing Notes", border_style="yellow"))


def print_pair_start(index: int, total: int, filename: str) -> None:
    console.rule(f"[bold blue][{index}/{total}] {filename}[/bold blue]")


def print_pair_plan(args: argparse.Namespace) -> None:
    lines = [
        "[cyan]CSV integrity[/cyan] and timestamp checks",
        "[cyan]Video metadata[/cyan] probe against original and CSV",
    ]
    if args.decode_coverage_pct > 0:
        lines.append(
            f"[cyan]Partial decode[/cyan]: {args.decode_coverage_pct:.2f}% across {args.decode_segments} segment(s)"
        )
    if args.burst_anchors > 0 and args.burst_length > 0:
        lines.append(
            f"[cyan]Burst compare[/cyan]: {args.burst_anchors} anchor(s) x {args.burst_length} frame(s)"
        )
    if args.full_decode:
        lines.append("[cyan]Full decode[/cyan] enabled")
    console.print("\n".join(f"  • {line}" for line in lines))


def main() -> int:
    args = parse_args()

    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        raise RuntimeError("ffprobe and ffmpeg must be installed and in PATH")

    compressed_dir = normalize_path(args.compressed_dir)
    originals_dir = normalize_path(args.originals_dir)

    if not compressed_dir.exists():
        raise FileNotFoundError(f"Compressed directory not found: {compressed_dir}")
    if not originals_dir.exists():
        raise FileNotFoundError(f"Originals directory not found: {originals_dir}")

    pairs, notes = pair_inputs(compressed_dir, originals_dir)

    print_header(compressed_dir, originals_dir, len(pairs))
    if notes:
        print_pairing_notes(notes)

    if not pairs:
        console.print("[red]No matching compressed/original pairs found.[/red]")
        return 1

    console.print()
    results: list[VerificationResult] = []
    total_pairs = len(pairs)
    for index, pair in enumerate(pairs, start=1):
        print_pair_start(index, total_pairs, pair.compressed_video.name)
        print_pair_plan(args)
        result = verify_pair(pair, args)
        results.append(result)
        print_result(result)

    pass_count = sum(result.status == "PASS" for result in results)
    warn_count = sum(result.status == "WARN" for result in results)
    fail_count = sum(result.status == "FAIL" for result in results)

    print_summary(pass_count, warn_count, fail_count)

    if fail_count:
        console.print("[red]At least one compressed output failed verification.[/red]")
        return 1

    if warn_count:
        console.print(
            "[yellow]Verification passed with warnings. Review the WARN items before deleting originals.[/yellow]"
        )
    else:
        console.print("[green]All matched outputs passed verification.[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
