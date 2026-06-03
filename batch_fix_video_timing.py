#!/usr/bin/env python3

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from fix_video_timing import (
    VIDEO_RE,
    NearbyCsvCandidate,
    analyze,
    build_prefixed_name,
    build_rewrite_plan,
    find_nearby_csv_candidate,
    infer_matching_csv,
    print_report,
    rewrite_and_compress,
    write_gzipped_csv_copy,
)


SUPPORTED_EXTS = {".avi", ".mp4", ".mov", ".mkv"}
console = Console()


@dataclass
class BatchResult:
    video: Path
    status: str
    detail: str
    expected_csv: Path | None = None
    nearby_csv: NearbyCsvCandidate | None = None


def render_result(result: BatchResult) -> None:
    if result.status == "failed" and result.expected_csv is not None:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Video", result.video.name)
        table.add_row("Expected CSV", str(result.expected_csv))

        if result.nearby_csv is not None:
            direction = "after" if result.nearby_csv.delta_s > 0 else "before"
            table.add_row("Likely CSV", str(result.nearby_csv.path))
            table.add_row(
                "Mismatch",
                (
                    f"CSV filename is {abs(result.nearby_csv.delta_s):.0f}s "
                    f"{direction} the AVI filename"
                ),
            )
            table.add_row(
                "Fix",
                f"If this CSV belongs to the video, rename it to {result.expected_csv.name}",
            )
        else:
            table.add_row(
                "Likely issue",
                "No same-prefix CSV was found within 1 second of the AVI timestamp",
            )
            table.add_row(
                "Fix",
                "Place the matching CSV next to the video, or provide --fps to compress without CSV timing",
            )

        console.print(
            Panel(
                table,
                title="[bold red]Missing timestamp CSV[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        return

    style = "red" if result.status == "failed" else "green"
    console.print(
        f"[{style}][{result.status}][/{style}] {result.video.name}: {result.detail}"
    )


def iter_source_videos(folder: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_EXTS:
            continue
        if not VIDEO_RE.match(path.name):
            continue
        candidates.append(path)
    return sorted(candidates)


def build_output_paths(video_path: Path, output_dir: Path) -> tuple[Path, Path]:
    output_video_name = build_prefixed_name(video_path).with_suffix(".mp4").name
    output_video = output_dir / output_video_name
    output_csv = output_video.with_suffix(".csv.gz")
    return output_video, output_csv


def move_file(src: Path, dst: Path, *, overwrite: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if not overwrite:
            raise FileExistsError(f"Destination already exists: {dst}")
        dst.unlink()
    shutil.move(str(src), str(dst))


def archive_inputs(
    video_path: Path, csv_path: Path, archive_dir: Path, *, overwrite: bool
) -> list[Path]:
    archived: list[Path] = []

    for path in (video_path, csv_path):
        if not path.exists():
            continue
        destination = archive_dir / path.name
        move_file(path, destination, overwrite=overwrite)
        archived.append(destination)

    return archived


def adopt_existing_output(
    video_path: Path,
    csv_path: Path,
    output_video: Path,
    output_csv: Path,
    *,
    archive_originals: bool,
    archive_dir: Path,
    overwrite: bool,
) -> BatchResult | None:
    existing_output = video_path.parent / output_video.name
    existing_output_csv = existing_output.with_suffix(".csv.gz")

    if existing_output == output_video or not existing_output.exists():
        return None

    print(f"\nAdopting existing output for {video_path.name}")
    move_file(existing_output, output_video, overwrite=overwrite)

    details = [f"moved existing mp4 to {output_video}"]

    if existing_output_csv.exists():
        move_file(existing_output_csv, output_csv, overwrite=overwrite)
        details.append(f"moved existing csv.gz to {output_csv}")

    if archive_originals:
        archived = archive_inputs(
            video_path, csv_path, archive_dir, overwrite=overwrite
        )
        if archived:
            details.append(f"archived inputs to {archive_dir}")

    return BatchResult(video_path, "adopted", "; ".join(details))


def process_video(
    video_path: Path,
    output_dir: Path,
    *,
    fps: float | None,
    round_fps: bool,
    crf: int,
    preset: str,
    dry_run: bool,
    archive_originals: bool,
    archive_dir: Path,
    overwrite: bool,
    adopt_existing: bool,
) -> BatchResult:
    csv_path = infer_matching_csv(video_path)
    output_video, output_csv = build_output_paths(video_path, output_dir)

    if not csv_path.exists() and fps is None:
        return BatchResult(
            video_path,
            "failed",
            "missing timestamp CSV",
            expected_csv=csv_path,
            nearby_csv=find_nearby_csv_candidate(video_path),
        )

    if adopt_existing:
        adopted = adopt_existing_output(
            video_path=video_path,
            csv_path=csv_path,
            output_video=output_video,
            output_csv=output_csv,
            archive_originals=archive_originals,
            archive_dir=archive_dir,
            overwrite=overwrite,
        )
        if adopted is not None:
            return adopted

    if output_video.exists() and not overwrite:
        return BatchResult(
            video_path, "skipped", f"output already exists: {output_video}"
        )

    print(f"\n{'=' * 80}")
    print(f"Processing: {video_path.name}")
    print(f"Output mp4: {output_video}")
    print(f"Output csv: {output_csv}")

    stats = analyze(video_path, csv_path)
    print_report(stats)

    plan = build_rewrite_plan(
        stats=stats,
        user_fps=fps,
        round_fps=round_fps,
    )
    print("\nRewrite plan:")
    print(f"  output fps:                  {plan.fps_out}")
    print(f"  setpts factor:               {plan.pts_factor:.9f}")

    if dry_run:
        return BatchResult(video_path, "dry-run", "analysis complete, no files written")

    output_dir.mkdir(parents=True, exist_ok=True)
    rewrite_and_compress(
        input_video=video_path,
        output_video=output_video,
        fps_out=plan.fps_out,
        pts_factor=plan.pts_factor,
        crf=crf,
        preset=preset,
    )

    if csv_path.exists():
        write_gzipped_csv_copy(csv_path, output_csv)

    details = [f"wrote {output_video.name}"]
    if csv_path.exists():
        details.append(f"wrote {output_csv.name}")

    if archive_originals:
        archived = archive_inputs(
            video_path, csv_path, archive_dir, overwrite=overwrite
        )
        if archived:
            details.append(f"archived inputs to {archive_dir}")

    return BatchResult(video_path, "processed", "; ".join(details))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch wrapper around fix_video_timing.py for a folder of timestamped videos."
    )
    parser.add_argument(
        "--folder",
        type=Path,
        default=Path("."),
        help="Folder containing the source videos and timestamp CSVs. Defaults to the current directory.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("compressed"),
        help="Directory where compressed mp4/csv.gz files are written. Defaults to ./compressed.",
    )
    parser.add_argument(
        "--archive-originals",
        action="store_true",
        help="After a successful encode, move the original video and source CSV into ./compressed/originals.",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=None,
        help="Archive directory for originals. Defaults to <output-dir>/originals when --archive-originals is used.",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help="Override output fps for every file. By default the wrapper uses the observed fps from the CSV.",
    )
    parser.add_argument(
        "--round-fps",
        action="store_true",
        help="Round observed fps to the nearest integer. Default is to keep the exact observed fps.",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=23,
        help="ffmpeg CRF value. Lower means larger output.",
    )
    parser.add_argument("--preset", type=str, default="medium", help="ffmpeg preset.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing compressed output or archive destination.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze every file and print the rewrite plan without writing outputs.",
    )
    parser.add_argument(
        "--no-adopt-existing",
        action="store_true",
        help="Do not move already-created mp4/csv.gz outputs from the source folder into the output directory.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("ffmpeg and ffprobe must be installed and in PATH")

    folder = args.folder.resolve()
    output_dir = (
        (folder / args.output_dir).resolve()
        if not args.output_dir.is_absolute()
        else args.output_dir
    )

    archive_dir = args.archive_dir
    if archive_dir is None:
        archive_dir = output_dir / "originals"
    elif not archive_dir.is_absolute():
        archive_dir = (folder / archive_dir).resolve()

    videos = iter_source_videos(folder)
    if not videos:
        print(f"No matching source videos found in {folder}")
        return

    console.print(f"Found {len(videos)} source video(s) in {folder}")
    console.print(f"Compressed outputs will go to {output_dir}")
    if args.archive_originals:
        console.print(f"Original inputs will be archived to {archive_dir}")

    results: list[BatchResult] = []
    for video_path in videos:
        try:
            result = process_video(
                video_path=video_path,
                output_dir=output_dir,
                fps=args.fps,
                round_fps=args.round_fps,
                crf=args.crf,
                preset=args.preset,
                dry_run=args.dry_run,
                archive_originals=args.archive_originals,
                archive_dir=archive_dir,
                overwrite=args.overwrite,
                adopt_existing=not args.no_adopt_existing,
            )
        except Exception as exc:  # noqa: BLE001
            result = BatchResult(video_path, "failed", str(exc))

        results.append(result)
        render_result(result)

    print(f"\n{'=' * 80}")
    for status in ("processed", "adopted", "skipped", "dry-run", "failed"):
        count = sum(1 for result in results if result.status == status)
        if count:
            print(f"{status}: {count}")


if __name__ == "__main__":
    main()
