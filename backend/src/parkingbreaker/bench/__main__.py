"""Run a resumable qualification bench using environment-only credentials."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Never

from parkingbreaker.bench.cases import parse_answer_key
from parkingbreaker.bench.cli import (
    PRESETS,
    SEARCHES,
    BenchError,
    run_bench,
    score_bench,
    validate_arguments,
)
from parkingbreaker.bench.key_review import (
    audit_key_row,
    load_saved_pages,
    render_review_packet,
    sign_key_rows,
    write_answer_key_atomic,
)
from parkingbreaker.http_fetcher import HttpFetcher


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise BenchError("invalid command line; see --help")


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    validate_arguments(argv)
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", allow_abbrev=False)
    run.add_argument("--search", choices=SEARCHES, default="brave")
    run.add_argument("--preset", choices=PRESETS, required=True)
    run.add_argument("--cases", type=Path, required=True)
    run.add_argument("--repeat", type=int, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--cap-usd", type=Decimal, required=True)
    run.add_argument(
        "--fake", action="store_true", help="offline synthetic seams; zero actual spend"
    )
    run.add_argument("--case", action="append", nargs="+", default=[], metavar="ID")
    run.add_argument("--snapshot", type=Path, help="default: OUT/dotgov-2026-10-13.csv (live)")
    score = commands.add_parser("score", allow_abbrev=False)
    score.add_argument("--runs", type=Path, required=True)
    score.add_argument("--cases", type=Path, required=True)
    score.add_argument("--out", type=Path, required=True)
    score.add_argument("--production-use", choices=PRESETS, action="extend", nargs="+", default=[])
    verify = commands.add_parser("verify-keys", allow_abbrev=False)
    verify.add_argument("--cases", type=Path, required=True)
    verify.add_argument("--pages-dir", type=Path, action="append", default=[])
    verify.add_argument("--save-dir", type=Path)
    verify.add_argument("--fetch-missing", action="store_true")
    verify.add_argument("--out", type=Path, required=True)
    sign = commands.add_parser("sign-key", allow_abbrev=False)
    sign.add_argument("--cases", type=Path, required=True)
    sign.add_argument("--pages-dir", type=Path, action="append", required=True)
    sign.add_argument("--case", action="append", required=True, metavar="ID")
    sign.add_argument("--owner", required=True)
    sign.add_argument("--status", choices=("certain", "uncertain", "excluded"))
    sign.add_argument("--clear-uncertain", action="store_true")
    sign.add_argument("--keep-uncertain", action="append", default=[], metavar="FIELD")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    command = None
    try:
        args = parse_arguments(sys.argv[1:] if argv is None else argv)
        command = args.command
        if command == "verify-keys":
            if args.out.resolve() == args.cases.resolve() or (
                args.out.exists() and args.out.samefile(args.cases)
            ):
                raise ValueError("review packet must not overwrite the answer key")
            rows = parse_answer_key(args.cases.read_text(encoding="utf-8"))
            directories = [*args.pages_dir]
            if args.save_dir is not None:
                directories.append(args.save_dir)
            pages = load_saved_pages(directories)
            fetcher = HttpFetcher() if args.fetch_missing else None
            clock = (lambda: datetime.now(UTC)) if args.fetch_missing else None
            audits = tuple(
                audit_key_row(row, pages, fetcher=fetcher, clock=clock, save_dir=args.save_dir)
                for row in rows
            )
            args.out.write_text(render_review_packet(audits, rows), encoding="utf-8")
            return 0
        if command == "sign-key":
            rows = parse_answer_key(args.cases.read_text(encoding="utf-8"))
            signed = sign_key_rows(
                rows,
                load_saved_pages(args.pages_dir),
                case_ids=args.case,
                owner=args.owner,
                signed_at=datetime.now(UTC),
                set_status=args.status,
                clear_uncertain=args.clear_uncertain,
                keep_uncertain_fields=args.keep_uncertain,
            )
            write_answer_key_atomic(args.cases, signed)
            return 0
        if args.command == "score":
            score_bench(
                runs=args.runs, cases=args.cases, out=args.out, production_use=args.production_use
            )
            return 0
        result = run_bench(
            preset=args.preset,
            cases=args.cases,
            repeat=args.repeat,
            out=args.out,
            cap_usd=args.cap_usd,
            fake=args.fake,
            search=args.search,
            case_ids=[identity for group in args.case for identity in group],
            snapshot=args.snapshot,
        )
    except BenchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        message = (
            str(exc)
            if command in {"verify-keys", "sign-key"}
            else "bench failed; check local inputs and configuration"
        )
        print(message, file=sys.stderr)
        return 2
    except (OSError, InvalidOperation, subprocess.SubprocessError):
        print("bench failed; check local inputs and configuration", file=sys.stderr)
        return 2
    print(
        f"completed={result.completed} skipped={result.skipped} "
        f"cap_stopped={result.cap_stopped} recorded_usd={result.spent_cents / 100}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
