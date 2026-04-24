#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

import batch_fix_video_timing
import fix_video_timing
import verify_compression


def dispatch(
    command_name: str,
    command_main: Callable[[], int | None],
    args: list[str],
) -> int:
    original_argv = sys.argv[:]
    try:
        sys.argv = [command_name, *args]
        result = command_main()
    finally:
        sys.argv = original_argv
    return 0 if result is None else int(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Wrapper entrypoint for Bonsai AVI compression and verification tools."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    compress_parser = subparsers.add_parser(
        "compress",
        help="Batch rewrite and compress videos using batch_fix_video_timing.py",
    )
    compress_parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to batch_fix_video_timing.py",
    )

    single_parser = subparsers.add_parser(
        "compress-one",
        help="Run fix_video_timing.py for a single video",
    )
    single_parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to fix_video_timing.py",
    )

    verify_parser = subparsers.add_parser(
        "verify",
        help="Verify compressed outputs against archived originals",
    )
    verify_parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to verify_compression.py",
    )

    return parser


def cli() -> int:
    parser = build_parser()
    parsed = parser.parse_args()

    forwarded_args = parsed.args
    if forwarded_args and forwarded_args[0] == "--":
        forwarded_args = forwarded_args[1:]

    if parsed.command == "compress":
        return dispatch(
            "batch_fix_video_timing.py",
            batch_fix_video_timing.main,
            forwarded_args,
        )

    if parsed.command == "compress-one":
        return dispatch(
            "fix_video_timing.py",
            fix_video_timing.main,
            forwarded_args,
        )

    if parsed.command == "verify":
        return dispatch(
            "verify_compression.py",
            verify_compression.main,
            forwarded_args,
        )

    parser.error(f"Unknown command: {parsed.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(cli())
