#!/usr/bin/env python3
"""Append a durable development handoff. Reads source metadata; never runs providers.

The task ledger remains the acceptance authority. This journal preserves decisions,
observed failures and exact next actions across accounts without relying on chat memory.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from zoneinfo import ZoneInfo

KIT = Path(__file__).resolve().parents[1]
APP = KIT.parent
PROJECT = APP.parent
JOURNAL = KIT / "state/development-journal.jsonl"
STATUS = APP / "STATUS.md"
BEGIN = "<!-- BEGIN TASK JOURNAL -->"
END = "<!-- END TASK JOURNAL -->"


def render_status(text: str, entries: list[dict]) -> str:
    """Replace only the journal projection; preserve the owner's surrounding text."""
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise ValueError("STATUS.md needs exactly one task-journal marker pair")
    before, remaining = text.split(BEGIN)
    _, after = remaining.split(END)
    blocks = []
    for entry in entries:
        if entry.get("phase") not in {"before", "during", "after"}:
            continue
        local = datetime.fromisoformat(entry["recorded_at"]).astimezone(
            ZoneInfo("America/Los_Angeles")
        )
        blocks.append(
            f"### {entry['task_id']} — {entry['phase']} — "
            f"{local:%Y-%m-%d %H:%M %Z} (checkpoint {entry['sequence']})\n\n"
            f"{entry['summary']}\n\n"
            f"Source revision: `{entry['git_head'][:12]}`; "
            f"fingerprint: `{entry['application_fingerprint']}`."
        )
        for key, label in (
            ("planned_checks", "Planned checks"),
            ("completed", "Completed"),
            ("observed_checks", "Observed checks"),
            ("unresolved", "Unresolved"),
            ("next_actions", "Next actions"),
        ):
            if entry.get(key):
                blocks.append(label + ":\n\n" + "\n".join("- " + x for x in entry[key]))
    return before + BEGIN + "\n\n" + "\n\n".join(blocks) + "\n\n" + END + after


def validate_phase(entry: dict, existing: list[dict]) -> None:
    if "phase" not in entry:
        return  # Preserve the original JSON checkpoint interface.
    phase, task_id = entry["phase"], entry.get("task_id", "")
    for key in ("planned_checks", "observed_checks"):
        values = entry.get(key, [])
        if not isinstance(values, list) or not all(isinstance(x, str) and x.strip() for x in values):
            raise ValueError("Task checks must be nonempty text entries")
    if phase not in {"before", "during", "after"} or not isinstance(task_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", task_id
    ):
        raise ValueError("Use a valid task ID and before/during/after phase")
    previous = next((e for e in reversed(existing) if "phase" in e), None)
    active = previous if previous and previous["phase"] != "after" else None
    if phase == "before":
        if active:
            raise ValueError("Finish the active task first: " + active["task_id"])
        if not entry.get("planned_checks"):
            raise ValueError("Before a task, record at least one planned check")
    elif active is None or active["task_id"] != task_id:
        raise ValueError("Record before for this task before during/after")
    if phase == "after" and active["phase"] != "during":
        raise ValueError("Record during progress before after")
    if phase == "after" and not entry["observed_checks"]:
        raise ValueError("After a task, record actual check results")


def write_status(text: str) -> None:
    if STATUS.is_symlink():
        raise ValueError("STATUS.md must be a regular workspace file")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".STATUS-", dir=STATUS.parent, delete=False
        ) as handle:
            temp = Path(handle.name)
            os.fchmod(handle.fileno(), STATUS.stat().st_mode & 0o777)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(STATUS)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def validate_history(entries: list[dict]) -> None:
    for index, entry in enumerate(entries):
        validate_phase(entry, entries[:index])


def validate_journal_path() -> None:
    if JOURNAL.is_symlink() or not JOURNAL.resolve().is_relative_to(APP.resolve()):
        raise ValueError("Development journal must be a regular workspace file")


def status_errors() -> list[str]:
    """Detect an interrupted journal-to-Markdown projection without changing files."""
    try:
        validate_journal_path()
        entries = [json.loads(line) for line in JOURNAL.read_text().splitlines()]
        validate_history(entries)
        text = STATUS.read_text()
        if STATUS.is_symlink() or render_status(text, entries) != text:
            return ["STATUS.md journal is out of sync; run checkpoint.py --sync-status"]
        return []
    except (OSError, ValueError, KeyError) as exc:
        return ["Living journal check failed: " + str(exc)]


def sync_status() -> None:
    validate_journal_path()
    with JOURNAL.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        entries = [json.loads(line) for line in handle]
        validate_history(entries)
        write_status(render_status(STATUS.read_text(), entries))


def checkpoint(entry_file: Path) -> dict:
    entry = json.loads(entry_file.read_text())
    return record(entry)


def record(entry: dict) -> dict:
    entry = dict(entry)
    for field in ("session", "summary"):
        if not isinstance(entry.get(field), str) or not entry[field].strip():
            raise ValueError("Missing checkpoint field: " + field)
    for field in ("completed", "observed_checks", "unresolved", "next_actions", "preserve"):
        if not isinstance(entry.get(field), list) or not all(isinstance(x, str) for x in entry[field]):
            raise ValueError("Checkpoint needs a string list: " + field)
    if not entry["next_actions"] or any(not x.strip() for x in entry["next_actions"]):
        raise ValueError("Record an exact next action")
    ledger = json.loads((KIT / "state/progress.json").read_text())["tasks"]
    spec = importlib.util.spec_from_file_location("parkingbreaker_kit", KIT / "scripts/kit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Hash only explicitly named source/evidence files. Never read environment values.
    snapshots = []
    for name in entry.pop("snapshot_files", []):
        path = (PROJECT / name).resolve()
        if not path.is_relative_to(PROJECT) or not path.is_file() or path.name.startswith(".env"):
            raise ValueError("Invalid checkpoint source/evidence path")
        snapshots.append({"path": str(path.relative_to(PROJECT)),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    entry.update(
        schema_version=1,
        recorded_at=datetime.now(UTC).isoformat(),
        repository=str(APP),
        git_head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=APP, text=True).strip(),
        application_fingerprint=module.application_fingerprint(),
        accepted_tasks=[key for key, value in ledger.items() if value["status"] == "accepted"],
        active_tasks=[key for key, value in ledger.items() if value["status"] == "in_progress"],
        source_snapshots=snapshots,
    )
    JOURNAL.parent.mkdir(exist_ok=True)
    validate_journal_path()
    with JOURNAL.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        existing = handle.readlines()
        # Refuse a partial/corrupt tail rather than silently losing another handoff.
        parsed = [json.loads(line) for line in existing]
        validate_history(parsed)
        validate_phase(entry, parsed)
        entry["sequence"] = len(existing) + 1
        # Prepare and validate the projection before appending. If a later disk write
        # fails, --sync-status repairs Markdown from this durable, append-only record.
        projected = render_status(STATUS.read_text(), [*parsed, entry]) if "phase" in entry else None
        if projected is not None and STATUS.is_symlink():
            raise ValueError("STATUS.md must be a regular workspace file")
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        if projected is not None:
            write_status(projected)
    return {"journal": str(JOURNAL.relative_to(APP)), "sequence": entry["sequence"],
            "active_tasks": entry["active_tasks"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entry", nargs="?", type=Path, help="Existing sanitized JSON checkpoint")
    parser.add_argument("--task", help="Task identifier, for example T21 or WORKSPACE")
    parser.add_argument("--phase", choices=["before", "during", "after"])
    parser.add_argument("--summary")
    parser.add_argument("--plan-check", action="append", default=[])
    parser.add_argument("--done", action="append", default=[])
    parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--gap", action="append", default=[])
    parser.add_argument("--next", dest="next_actions", action="append", default=[])
    parser.add_argument("--sync-status", action="store_true")
    args = parser.parse_args()
    try:
        structured = any((args.task, args.phase, args.summary, args.next_actions,
                          args.plan_check, args.done, args.check, args.gap))
        if args.sync_status:
            if args.entry or structured:
                raise ValueError("--sync-status cannot also append an entry")
            sync_status()
            result = {"status_synchronized": True}
        elif args.entry:
            if structured:
                raise ValueError("Choose a JSON entry or task phase flags")
            result = checkpoint(args.entry)
        else:
            if not all((args.task, args.phase, args.summary, args.next_actions)):
                raise ValueError("Provide --task, --phase, --summary and at least one --next")
            result = record({
                "session": "Task " + args.task,
                "task_id": args.task, "phase": args.phase, "summary": args.summary,
                "completed": args.done, "observed_checks": args.check,
                "planned_checks": args.plan_check, "unresolved": args.gap,
                "next_actions": args.next_actions, "preserve": [],
            })
        print(json.dumps(result))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    main()
