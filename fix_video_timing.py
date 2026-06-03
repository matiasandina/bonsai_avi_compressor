#!/usr/bin/env python3
from __future__ import annotations
import argparse
import gzip
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd


def gzip_csv_inplace(csv_path: Path) -> Path:
    if csv_path.suffix == ".gz":
        return csv_path
    gz_path = csv_path.with_suffix(csv_path.suffix + ".gz")
    with open(csv_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    csv_path.unlink()  # remove original
    return gz_path


VIDEO_RE = re.compile(
    r"^(?P<prefix>.+?)_(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}_\d{2}_\d{2})\.(?P<ext>avi|mp4|mov|mkv)$",
    re.IGNORECASE,
)


@dataclass
class VideoStats:
    path: Path

    ffprobe_fps: Optional[float]

    ffprobe_nb_frames: Optional[int]

    ffprobe_duration_s: Optional[float]

    duration_from_timestamps_s: Optional[float]

    observed_fps_from_timestamps: Optional[float]

    csv_rows: int


@dataclass
class RewritePlan:
    fps_out: float

    pts_factor: float


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:

    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def parse_fraction(rate_str: Optional[str]) -> Optional[float]:

    if not rate_str or rate_str == "0/0":
        return None

    if "/" in rate_str:
        num, den = rate_str.split("/")

        den_f = float(den)

        if den_f == 0:
            return None

        return float(num) / den_f

    return float(rate_str)


def ffprobe_stream_info(
    video_path: Path,
) -> tuple[Optional[float], Optional[int], Optional[float]]:

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=r_frame_rate,avg_frame_rate,nb_frames,duration",
        "-of",
        "json",
        str(video_path),
    ]

    result = run_cmd(cmd)

    data = json.loads(result.stdout)

    streams = data.get("streams", [])

    if not streams:
        return None, None, None

    stream = streams[0]

    avg_fps = parse_fraction(stream.get("avg_frame_rate"))

    r_fps = parse_fraction(stream.get("r_frame_rate"))

    fps = avg_fps if avg_fps and avg_fps > 0 else r_fps

    nb_frames = stream.get("nb_frames")

    nb_frames_int = int(nb_frames) if nb_frames not in (None, "N/A") else None

    duration = stream.get("duration")

    duration_f = float(duration) if duration not in (None, "N/A") else None

    return fps, nb_frames_int, duration_f


def read_timestamp_csv(csv_path: Path) -> pd.DataFrame:

    df = pd.read_csv(csv_path, header=None, names=["local_ts", "utc_ts"])

    df["local_ts"] = pd.to_datetime(df["local_ts"], errors="coerce")

    df["utc_ts"] = pd.to_datetime(df["utc_ts"], errors="coerce", utc=True)

    return df


def estimate_fps_from_timestamps(
    df: pd.DataFrame,
) -> tuple[Optional[float], Optional[float], int]:

    valid = df.dropna(subset=["local_ts"]).copy()

    n = len(valid)

    if n < 2:
        return None, None, n

    start = valid["local_ts"].iloc[0]

    end = valid["local_ts"].iloc[-1]

    duration_s = (end - start).total_seconds()

    if duration_s <= 0:
        return None, None, n

    fps = (n - 1) / duration_s

    return fps, duration_s, n


def build_prefixed_name(video_path: Path) -> Path:

    m = VIDEO_RE.match(video_path.name)

    if not m:
        return video_path.with_name(f"{video_path.stem}_renamed{video_path.suffix}")

    prefix = m.group("prefix")

    ts = m.group("ts")

    ext = m.group("ext")

    new_name = f"{ts}_{prefix}.{ext.lower()}"

    return video_path.with_name(new_name)


def infer_matching_csv(video_path: Path) -> Path:

    csv_path = video_path.with_suffix(".csv")

    csv_gz_path = video_path.with_suffix(".csv.gz")

    if csv_path.exists():
        return csv_path

    if csv_gz_path.exists():
        return csv_gz_path

    return csv_path


def parse_video_identity(path: Path) -> tuple[str, datetime] | None:
    match = VIDEO_RE.match(path.name)
    if not match:
        return None
    timestamp = datetime.strptime(match.group("ts"), "%Y-%m-%dT%H_%M_%S")
    return match.group("prefix"), timestamp


def find_nearby_csv_candidate(
    video_path: Path,
    *,
    tolerance_s: float = 1.0,
) -> Path | None:
    identity = parse_video_identity(video_path)
    if identity is None:
        return None

    video_prefix, video_ts = identity
    candidates: list[tuple[float, Path]] = []
    for csv_path in video_path.parent.glob(f"{video_prefix}_*.csv*"):
        csv_stem = csv_path.name
        if csv_stem.endswith(".csv.gz"):
            csv_stem = csv_stem[:-7]
        elif csv_stem.endswith(".csv"):
            csv_stem = csv_stem[:-4]
        else:
            continue

        fake_video_name = f"{csv_stem}.avi"
        csv_identity = parse_video_identity(csv_path.with_name(fake_video_name))
        if csv_identity is None:
            continue
        csv_prefix, csv_ts = csv_identity
        if csv_prefix != video_prefix:
            continue

        delta_s = abs((csv_ts - video_ts).total_seconds())
        if delta_s <= tolerance_s:
            candidates.append((delta_s, csv_path))

    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1].name))[0][1]


def missing_csv_message(video_path: Path, expected_csv: Path) -> str:
    nearby = find_nearby_csv_candidate(video_path)
    if nearby is None:
        return (
            f"missing timestamp CSV: expected {expected_csv}. "
            "Provide --fps to compress without CSV timing, or place the matching CSV next to the video."
        )

    suggested_name = expected_csv.name
    return (
        f"missing timestamp CSV: expected {expected_csv}. "
        f"Found a likely 1-second timestamp mismatch: {nearby}. "
        f"Fix: if this CSV belongs to the video, rename it to {suggested_name} "
        "or pass it explicitly with --csv."
    )


def analyze(video_path: Path, csv_path: Path) -> VideoStats:

    ffprobe_fps, ffprobe_nb_frames, ffprobe_duration_s = ffprobe_stream_info(video_path)

    if not csv_path.exists():
        return VideoStats(
            path=video_path,
            ffprobe_fps=ffprobe_fps,
            ffprobe_nb_frames=ffprobe_nb_frames,
            ffprobe_duration_s=ffprobe_duration_s,
            duration_from_timestamps_s=None,
            observed_fps_from_timestamps=None,
            csv_rows=0,
        )

    df = read_timestamp_csv(csv_path)

    observed_fps, duration_s, csv_rows = estimate_fps_from_timestamps(df)

    print(f"Timestamps summary from CSV: {csv_path}")

    print("-----------------------------------------")

    summarize_intervals(df)

    print("-----------------------------------------")

    return VideoStats(
        path=video_path,
        ffprobe_fps=ffprobe_fps,
        ffprobe_nb_frames=ffprobe_nb_frames,
        ffprobe_duration_s=ffprobe_duration_s,
        duration_from_timestamps_s=duration_s,
        observed_fps_from_timestamps=observed_fps,
        csv_rows=csv_rows,
    )


def print_report(stats: VideoStats) -> None:

    print(f"\nVideo: {stats.path}")

    print(f"  ffprobe fps:                 {stats.ffprobe_fps}")

    print(f"  ffprobe nb_frames:           {stats.ffprobe_nb_frames}")

    print(f"  ffprobe duration (s):        {stats.ffprobe_duration_s}")

    print(f"  csv rows:                    {stats.csv_rows}")

    print(f"  timestamp duration (s):      {stats.duration_from_timestamps_s}")

    print(f"  observed fps from csv:       {stats.observed_fps_from_timestamps}")

    if stats.ffprobe_nb_frames is not None and stats.csv_rows > 0:
        print(
            f"  ffprobe frames - csv rows:   {stats.ffprobe_nb_frames - stats.csv_rows}"
        )

    if stats.observed_fps_from_timestamps and stats.ffprobe_fps:
        ratio = stats.ffprobe_fps / stats.observed_fps_from_timestamps

        print(f"  metadata/observed fps ratio: {ratio:.3f}")

        if ratio > 1.2 or ratio < 0.8:
            print("  WARNING: metadata fps and observed fps differ materially.")

        else:
            print("  fps looks roughly consistent.")

    if stats.ffprobe_duration_s and stats.duration_from_timestamps_s:
        dur_ratio = stats.ffprobe_duration_s / stats.duration_from_timestamps_s

        print(f"  metadata/timestamp dur ratio:{dur_ratio:.3f}")


def rewrite_and_compress(
    input_video: Path,
    output_video: Path,
    fps_out: float,
    pts_factor: float,
    crf: int = 23,
    preset: str = "medium",
) -> None:

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_video),
        "-an",
        "-vf",
        f"setpts={pts_factor:.9f}*PTS",
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-r",
        f"{fps_out:.6f}",
        str(output_video),
    ]

    print("\nRunning:")

    print(" ".join(cmd))

    subprocess.run(cmd, check=True)


def summarize_intervals(df: pd.DataFrame) -> None:

    valid = df.dropna(subset=["local_ts"]).copy()

    dt = valid["local_ts"].diff().dropna().dt.total_seconds()

    if len(dt) == 0:
        print("No valid timestamp intervals.")

        return

    print(f"  interval mean (s):           {dt.mean()}")

    print(f"  interval median (s):         {dt.median()}")

    print(f"  interval std (s):            {dt.std()}")

    print(f"  interval min (s):            {dt.min()}")

    print(f"  interval p01 (s):            {dt.quantile(0.01)}")

    print(f"  interval p99 (s):            {dt.quantile(0.99)}")

    print(f"  interval max (s):            {dt.max()}")


def build_output_csv_path(
    output_video: Path,
) -> Path:

    return output_video.with_suffix(".csv.gz")


def write_gzipped_csv_copy(source_csv: Path, output_csv_gz: Path) -> Path:

    if source_csv.suffix == ".gz":
        shutil.copy2(source_csv, output_csv_gz)

        return output_csv_gz

    with open(source_csv, "rb") as f_in, gzip.open(output_csv_gz, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    return output_csv_gz


def build_rewrite_plan(
    stats: VideoStats,
    user_fps: Optional[float],
    round_fps: bool = True,
) -> RewritePlan:

    fps_out = choose_output_fps(
        user_fps=user_fps,
        observed_fps=stats.observed_fps_from_timestamps,
        round_fps=round_fps,
    )

    if stats.ffprobe_fps and fps_out > 0:
        pts_factor = stats.ffprobe_fps / fps_out

    elif stats.ffprobe_duration_s and stats.duration_from_timestamps_s:
        pts_factor = stats.duration_from_timestamps_s / stats.ffprobe_duration_s

    else:
        raise RuntimeError(
            "Cannot compute playback-speed correction. Need ffprobe fps or both "
            "ffprobe duration and timestamp duration."
        )

    if pts_factor <= 0:
        raise RuntimeError(f"Computed invalid PTS factor: {pts_factor}")

    return RewritePlan(fps_out=fps_out, pts_factor=pts_factor)


def main() -> None:

    parser = argparse.ArgumentParser()

    parser.add_argument("video", type=Path, help="Path to video file")

    parser.add_argument(
        "--csv", type=Path, default=None, help="Optional path to timestamp CSV"
    )

    parser.add_argument(
        "--rewrite", action="store_true", help="Rewrite/compress video after checks"
    )

    parser.add_argument("--fps", type=float, default=None, help="Override output fps")

    parser.add_argument("--crf", type=int, default=23)

    parser.add_argument("--preset", type=str, default="medium")

    parser.add_argument(
        "--no-round-fps",
        action="store_true",
        help="Use exact observed fps instead of rounding to nearest integer",
    )

    args = parser.parse_args()

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("ffmpeg and ffprobe must be installed and in PATH")

    video_path = args.video

    csv_path = args.csv if args.csv else infer_matching_csv(video_path)

    if not csv_path.exists() and args.fps is None:
        raise RuntimeError(missing_csv_message(video_path, csv_path))

    stats = analyze(video_path, csv_path)

    print_report(stats)

    output_name = build_prefixed_name(video_path).with_suffix(".mp4")

    output_csv = build_output_csv_path(
        output_video=output_name,
    )

    if not args.rewrite:
        return

    plan = build_rewrite_plan(
        stats=stats,
        user_fps=args.fps,
        round_fps=not args.no_round_fps,
    )

    print(f"\nRewrite plan:")

    print(f"  output fps:                  {plan.fps_out}")

    print(f"  setpts factor:               {plan.pts_factor:.9f}")

    rewrite_and_compress(
        input_video=video_path,
        output_video=output_name,
        fps_out=plan.fps_out,
        pts_factor=plan.pts_factor,
        crf=args.crf,
        preset=args.preset,
    )

    print(f"\nWrote: {output_name}")

    if csv_path.exists():
        csv_output_path = write_gzipped_csv_copy(csv_path, output_csv)

        print(f"Wrote timestamp sidecar: {csv_output_path}")


def choose_output_fps(
    user_fps: Optional[float],
    observed_fps: Optional[float],
    round_fps: bool = True,
) -> float:

    if user_fps is not None:
        return float(user_fps)

    if observed_fps is None:
        raise RuntimeError("No output fps available. Provide --fps or a valid CSV.")

    if round_fps:
        return float(round(observed_fps))

    return float(observed_fps)


if __name__ == "__main__":
    main()
