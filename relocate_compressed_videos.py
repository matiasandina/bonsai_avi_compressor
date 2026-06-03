#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


DATE_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})T")
SUPPORTED_ENDINGS = (".mp4", ".csv", ".csv.gz")


@dataclass
class Relocation:
    source: Path
    destination: Path
    status: str
    detail: str


def is_relocatable_file(path: Path) -> bool:
    name = path.name.lower()
    return path.is_file() and any(name.endswith(ending) for ending in SUPPORTED_ENDINGS)


def recording_date(path: Path) -> datetime:
    match = DATE_RE.match(path.name)
    if not match:
        raise ValueError(f"Filename does not start with YYYY-MM-DDT: {path.name}")
    return datetime.strptime(match.group("date"), "%Y-%m-%d")


def session_prefix(recorded_on: datetime, offset_days: int) -> str:
    session_date = recorded_on + timedelta(days=offset_days)
    return session_date.strftime("%Y_%m_%d")


def find_session_dir(
    target_root: Path,
    prefix: str,
    *,
    allow_ambiguous: bool,
) -> Path:
    matches = sorted(
        path for path in target_root.glob(f"{prefix}*") if path.is_dir()
    )
    if not matches:
        raise FileNotFoundError(
            f"No session folder found under {target_root} matching {prefix}*"
        )
    if len(matches) > 1 and not allow_ambiguous:
        match_list = "\n  ".join(str(path) for path in matches)
        raise RuntimeError(
            f"Multiple session folders match {prefix}*:\n  {match_list}\n"
            "Re-run with --allow-ambiguous to use the first sorted match."
        )
    return matches[0]


def iter_source_files(source_dir: Path) -> list[Path]:
    return sorted(path for path in source_dir.iterdir() if is_relocatable_file(path))


def relocate_file(
    source: Path,
    target_dir: Path,
    *,
    dry_run: bool,
    overwrite: bool,
) -> Relocation:
    destination = target_dir / source.name
    if destination.exists() and not overwrite:
        return Relocation(
            source=source,
            destination=destination,
            status="skipped",
            detail="destination already exists",
        )

    if dry_run:
        return Relocation(
            source=source,
            destination=destination,
            status="dry-run",
            detail="would move",
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    shutil.move(str(source), str(destination))
    return Relocation(
        source=source,
        destination=destination,
        status="moved",
        detail="moved",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Move timestamped compressed mp4/csv outputs into matching session "
            "rawvideo folders."
        )
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("compressed"),
        help="Folder containing YYYY-MM-DDT*.mp4/.csv/.csv.gz files. Defaults to ./compressed.",
    )
    parser.add_argument(
        "--target-root",
        type=Path,
        required=True,
        help="Root folder containing session directories named like YYYY_MM_DD...",
    )
    parser.add_argument(
        "--session-date-offset-days",
        type=int,
        default=0,
        help=(
            "Days to add to the recording date before matching session folders. "
            "Default 0 matches 2026-05-29T* to 2026_05_29*."
        ),
    )
    parser.add_argument(
        "--rawvideo-name",
        default="rawvideo",
        help="Name of the folder created inside each session directory. Defaults to rawvideo.",
    )
    parser.add_argument(
        "--allow-ambiguous",
        action="store_true",
        help="If multiple session folders match one date prefix, use the first sorted match.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace files that already exist in the destination rawvideo folder.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned moves without creating folders or moving files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    source_dir = args.source_dir.resolve()
    target_root = args.target_root.resolve()

    if not source_dir.is_dir():
        raise NotADirectoryError(f"Source directory does not exist: {source_dir}")
    if not target_root.is_dir():
        raise NotADirectoryError(f"Target root does not exist: {target_root}")

    files = iter_source_files(source_dir)
    if not files:
        print(f"No timestamped mp4/csv outputs found in {source_dir}")
        return

    session_dirs: dict[str, Path] = {}
    results: list[Relocation] = []

    for source in files:
        try:
            recorded_on = recording_date(source)
            prefix = session_prefix(recorded_on, args.session_date_offset_days)
            session_dir = session_dirs.get(prefix)
            if session_dir is None:
                session_dir = find_session_dir(
                    target_root,
                    prefix,
                    allow_ambiguous=args.allow_ambiguous,
                )
                session_dirs[prefix] = session_dir

            rawvideo_dir = session_dir / args.rawvideo_name
            result = relocate_file(
                source,
                rawvideo_dir,
                dry_run=args.dry_run,
                overwrite=args.overwrite,
            )
        except Exception as exc:  # noqa: BLE001
            result = Relocation(
                source=source,
                destination=Path(),
                status="failed",
                detail=str(exc),
            )

        results.append(result)
        destination = f" -> {result.destination}" if result.destination else ""
        print(f"[{result.status}] {result.source}{destination}: {result.detail}")

    print()
    for status in ("moved", "dry-run", "skipped", "failed"):
        count = sum(1 for result in results if result.status == status)
        if count:
            print(f"{status}: {count}")


if __name__ == "__main__":
    main()
