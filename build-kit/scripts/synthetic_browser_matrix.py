#!/usr/bin/env python3
"""Run the four required controlled-free beta browser projects as owned campaigns.

Each project receives its own fresh synthetic runtime/database and its own
independently validated rehearsal receipt/report; every project is bound to one
canonical application/harness/config source fingerprint. The aggregate receipt is
content-free and claims local synthetic qualification only: no provider, network,
real customer data, physical device or camera action is involved.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE_PHASE = "controlled_free_beta"
QUALIFICATION = "local synthetic only"
CAMPAIGN_TIMEOUT_SECONDS = 3600
PROCESS_ENVIRONMENT = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SYSTEMROOT", "PB_TICKET_READING_PREVIEW")


def kit_module():
    spec = importlib.util.spec_from_file_location("matrix_kit", ROOT / "build-kit/scripts/kit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KIT = kit_module()
CHECK_ID = KIT.synthetic_browser_check(SERVICE_PHASE)
PROJECTS = tuple(KIT.BETA_BROWSER_PROJECTS)
BETA_CONFIG = f"web/{KIT.BETA_BROWSER_CONFIG}"
REQUIRED_SPECS = tuple(sorted(KIT.required_browser_specs(CHECK_ID)))
HARNESS_FILES = (
    "build-kit/scripts/synthetic_api.py",
    "build-kit/scripts/kit.py",
    "build-kit/scripts/synthetic_rehearsal.py",
    "build-kit/scripts/synthetic_browser_matrix.py",
    BETA_CONFIG,
    "web/package.json",
    "web/package-lock.json",
    "web/tests/synthetic-fixtures.ts",
)


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str | None:
    try:
        return digest_bytes(path.read_bytes())
    except OSError:
        return None


def clean_environment() -> dict[str, str]:
    """Inherit only system settings and the explicit ticket-reading preview toggle."""
    return {key: os.environ[key] for key in PROCESS_ENVIRONMENT if key in os.environ}


def ticket_reading_preview() -> bool:
    return os.environ.get("PB_TICKET_READING_PREVIEW") == "1"


def required_specs() -> tuple[str, ...]:
    return tuple(sorted(KIT.required_browser_specs(CHECK_ID, ticket_reading_preview())))


def source_binding() -> dict:
    """One canonical application plus harness/config fingerprint for every project."""
    try:
        fingerprint = KIT.application_fingerprint()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(
            "Matrix source binding needs the git-backed application fingerprint"
        ) from error
    return {
        "application_fingerprint": fingerprint,
        "harness_sha256": {name: file_sha256(ROOT / name) for name in HARNESS_FILES},
    }


def project_errors(
    project: str,
    rehearsal: dict,
    receipt: dict,
    report_validation: dict,
    binding: dict,
) -> list[str]:
    """Fail a project unless its own preserved evidence proves the required campaign."""
    info = KIT.BETA_BROWSER_PROJECTS.get(project)
    if info is None:
        return ["Unknown beta browser project"]
    errors = []

    def require(condition, message) -> None:
        if not condition:
            errors.append(f"{project}: {message}")

    require(rehearsal.get("passed") is True, "rehearsal did not pass")
    require(rehearsal.get("synthetic_only") is True, "rehearsal is not synthetic-only")
    require(
        rehearsal.get("service_phase") == SERVICE_PHASE,
        "rehearsal used another service phase",
    )
    require(
        rehearsal.get("browser_project") == project,
        "rehearsal evidence belongs to another project",
    )
    require(
        rehearsal.get("application_fingerprint") == binding["application_fingerprint"],
        "rehearsal source fingerprint is stale",
    )
    require(
        rehearsal.get("code_unchanged_during_rehearsal") is True,
        "application source changed during the campaign",
    )
    require(
        rehearsal.get("harness_unchanged_during_rehearsal") is True,
        "harness source changed during the campaign",
    )
    require(
        rehearsal.get("owned_services_stopped") is True,
        "owned services were not reaped",
    )
    require(
        rehearsal.get("harness_sha256") == rehearsal.get("initial_harness_sha256"),
        "harness fingerprint was not stable",
    )
    require(
        rehearsal.get("harness_sha256") == binding["harness_sha256"],
        "harness/config source is stale or missing",
    )
    proxy = rehearsal.get("connect_proxy") or {}
    require(proxy.get("mode") == "http-connect-loopback", "CONNECT proxy is missing")
    require(
        proxy.get("allowed_destination") == "portal.example.test:443",
        "CONNECT proxy allowlist is wrong",
    )
    require(
        isinstance(proxy.get("tunnel_target"), str)
        and proxy["tunnel_target"].startswith("127.0.0.1:"),
        "CONNECT proxy tunnel target is not the owned loopback port",
    )
    require(proxy.get("stopped") is True, "owned CONNECT proxy was not reaped")
    require(isinstance(proxy.get("log_sha256"), str), "CONNECT proxy log is missing")

    require(receipt.get("check_id") == CHECK_ID, "receipt is not the controlled-free check")
    require(
        rehearsal.get("ticket_reading_preview", False) is ticket_reading_preview()
        and receipt.get("ticket_reading_preview", False) is ticket_reading_preview(),
        "ticket-reading preview mode is missing or wrong",
    )
    require(
        receipt.get("command") == KIT.beta_browser_check_command(project, ticket_reading_preview()),
        "receipt command does not bind this project",
    )
    require(
        receipt.get("browser_project") == project,
        "receipt project binding is missing or wrong",
    )
    require(receipt.get("observed") is True, "receipt is not observed")
    require(
        receipt.get("exit_code") == receipt.get("command_exit_code") == 0,
        "receipt is nonzero",
    )
    require(
        receipt.get("code_unchanged_during_check") is True,
        "source changed during the browser check",
    )
    require(
        receipt.get("application_fingerprint") == binding["application_fingerprint"],
        "receipt source fingerprint is stale",
    )
    stored = receipt.get("browser_report_validation") or {}
    require(stored.get("valid") is True, "stored report validation is invalid")
    require(stored == report_validation, "stored report validation is stale")
    require(
        isinstance(report_validation.get("tests"), int)
        and not isinstance(report_validation.get("tests"), bool)
        and report_validation["tests"] > 0,
        "report observed no passing tests",
    )
    return errors


def evidence_entry(
    project: str,
    rehearsal: dict,
    receipt: dict,
    report_validation: dict,
    hashes: dict,
) -> dict:
    """Compact content-free evidence: engine/project/viewport/counts and hashes."""
    info = KIT.BETA_BROWSER_PROJECTS[project]
    return {
        "project": project,
        "engine": info["engine"],
        "viewport": list(info["viewport"]),
        "tests": report_validation["tests"],
        "results": report_validation["results"],
        "rehearsal": f"{project}/rehearsal.json",
        "rehearsal_sha256": hashes["rehearsal"],
        "receipt": f"{project}/receipt.json",
        "receipt_sha256": hashes["receipt"],
        "report": f"{project}/report.json",
        "report_sha256": hashes["report"],
        "campaign_log_sha256": hashes["campaign"],
        "check_log_sha256": receipt.get("log_sha256"),
        "connect_proxy_log_sha256": (rehearsal.get("connect_proxy") or {}).get("log_sha256"),
    }


def aggregate_errors(entries: list[dict], expected: tuple[str, ...]) -> list[str]:
    """Exactly the four required projects, once each, with positive observed counts."""
    errors = []
    names = [entry.get("project") for entry in entries]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        errors.append("Duplicate browser project evidence: " + ", ".join(duplicates))
    missing = [name for name in expected if name not in names]
    if missing:
        errors.append("Missing required browser project: " + ", ".join(missing))
    unexpected = sorted(set(names) - set(expected))
    if unexpected:
        errors.append("Unexpected browser project: " + ", ".join(unexpected))
    if len(entries) != len(expected):
        errors.append("Matrix needs one evidence entry per required project")
    for entry in entries:
        if entry.get("project") not in expected:
            continue
        info = KIT.BETA_BROWSER_PROJECTS[entry["project"]]
        if entry.get("engine") != info["engine"]:
            errors.append(f"{entry['project']}: engine binding is wrong")
        if entry.get("viewport") != list(info["viewport"]):
            errors.append(f"{entry['project']}: viewport binding is wrong")
        tests = entry.get("tests")
        if not isinstance(tests, int) or isinstance(tests, bool) or tests <= 0:
            errors.append(f"{entry['project']}: no observed passing checks")
    return errors


def preserve(directory: Path, rehearsal: dict) -> tuple[dict, dict]:
    """Copy the rehearsal receipt and raw report beside the preserved rehearsal."""
    receipt_source = KIT.inside(ROOT, rehearsal["browser_check_receipt"])
    shutil.copyfile(receipt_source, directory / "receipt.json")
    receipt = json.loads((directory / "receipt.json").read_text())
    report_source = KIT.inside(KIT.KIT, receipt.get("browser_report", ""))
    shutil.copyfile(report_source, directory / "report.json")
    return receipt, KIT.browser_report_validation(
        directory / "report.json", required_specs(), receipt.get("browser_project")
    )


def campaign(project: str, directory: Path) -> int:
    """Run one independently owned synthetic rehearsal for exactly this project."""
    command = [
        sys.executable,
        str(ROOT / "build-kit/scripts/synthetic_rehearsal.py"),
        "--service-phase",
        SERVICE_PHASE,
        "--browser-project",
        project,
        "--out",
        str(directory),
    ]
    log = (directory.parent / f"{project}.campaign.log").open("w")
    try:
        child = subprocess.Popen(
            command,
            cwd=ROOT,
            env=clean_environment(),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            code = child.wait(timeout=CAMPAIGN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=60)
            code = -1
    finally:
        log.close()
    return code


def collect(
    project: str, directory: Path, binding: dict, code: int
) -> tuple[dict | None, list[str]]:
    """Validate one project's preserved evidence without trusting campaign exit alone."""
    rehearsal_path = directory / "rehearsal.json"
    if not rehearsal_path.is_file():
        return None, [f"{project}: missing rehearsal evidence"]
    try:
        rehearsal = json.loads(rehearsal_path.read_text())
        receipt, report_validation = preserve(directory, rehearsal)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return None, [f"{project}: unreadable evidence ({error})"]
    hashes = {
        "rehearsal": file_sha256(rehearsal_path),
        "receipt": file_sha256(directory / "receipt.json"),
        "report": file_sha256(directory / "report.json"),
        "campaign": file_sha256(directory.parent / f"{project}.campaign.log"),
    }
    errors = project_errors(project, rehearsal, receipt, report_validation, binding)
    if hashes["report"] != receipt.get("browser_report_sha256"):
        errors.append(f"{project}: preserved report hash does not match the receipt")
    try:
        check_log = KIT.inside(KIT.KIT, receipt.get("log", ""))
    except ValueError:
        check_log = None
    if check_log is None or file_sha256(check_log) != receipt.get("log_sha256"):
        errors.append(f"{project}: check log binding is stale")
    if code != 0:
        errors.append(f"{project}: campaign exited {code}")
    if None in hashes.values():
        errors.append(f"{project}: preserved evidence hash is missing")
    if errors:
        return None, errors
    return evidence_entry(project, rehearsal, receipt, report_validation, hashes), []


def run_matrix(output: Path) -> int:
    if ticket_reading_preview() and not (ROOT / "web" / KIT.TICKET_READING_SPEC).is_file():
        raise RuntimeError("Ticket-reading preview requires " + KIT.TICKET_READING_SPEC)
    output = output.absolute()
    if output.exists() or any(parent.is_symlink() for parent in (output, *output.parents)):
        raise RuntimeError("Matrix evidence destination must be a new path without symlinks")
    before = source_binding()
    output.mkdir(parents=True)
    started = time.time()
    result = {
        "matrix": "controlled-free-beta-four-projects",
        "service_phase": SERVICE_PHASE,
        "ticket_reading_preview": ticket_reading_preview(),
        "synthetic_only": True,
        "qualification": QUALIFICATION,
        "physical_camera": False,
        "mobile_viewport_emulation_only": True,
        "required_projects": list(PROJECTS),
        "required_specs": list(required_specs()),
        "source": before,
        "projects": [],
        "checks": {"projects": 0, "tests": 0},
        "cleanup": {
            "owned_services_stopped": False,
            "owned_connect_proxies_stopped": False,
        },
        "passed": False,
        "errors": [],
    }
    errors = []
    entries = []
    try:
        for project in PROJECTS:
            code = campaign(project, output / project)
            entry, project_failures = collect(project, output / project, before, code)
            errors.extend(project_failures)
            if entry is not None:
                entries.append(entry)
        errors.extend(aggregate_errors(entries, PROJECTS))
        after = source_binding()
        if after != before:
            errors.append("Application/harness/config source mutated during the matrix")
        rehearsals = []
        for project in PROJECTS:
            path = output / project / "rehearsal.json"
            if path.is_file():
                rehearsals.append(json.loads(path.read_text()))
        stopped = bool(rehearsals) and all(
            item.get("owned_services_stopped") is True for item in rehearsals
        )
        proxies = bool(rehearsals) and all(
            (item.get("connect_proxy") or {}).get("stopped") is True for item in rehearsals
        )
        result["source_after"] = after
        result["cleanup"] = {
            "owned_services_stopped": stopped,
            "owned_connect_proxies_stopped": proxies,
        }
        if not stopped or not proxies:
            errors.append("Owned campaign services were not all reaped")
        runtimes = [item.get("runtime") for item in rehearsals]
        if (
            len(runtimes) != len(PROJECTS)
            or any(not isinstance(item, str) for item in runtimes)
            or len(set(runtimes)) != len(runtimes)
        ):
            errors.append("Projects did not keep independently owned synthetic runtimes")
        result["projects"] = entries
        result["checks"] = {
            "projects": len(entries),
            "tests": sum(entry["tests"] for entry in entries),
            "required_specs": len(required_specs()),
        }
        result["passed"] = not errors and len(entries) == len(PROJECTS)
    except (
        RuntimeError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.TimeoutExpired,
    ) as error:
        errors.append(str(error))
    finally:
        result["errors"] = errors
        result["passed"] = result["passed"] and not errors
        result["duration_seconds"] = time.time() - started
        (output / "matrix.json").write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "passed": result["passed"],
                    "qualification": QUALIFICATION,
                    "checks": result["checks"],
                    "evidence": str(output / "matrix.json"),
                }
            ),
            flush=True,
        )
    return 0 if result["passed"] else 1


def preflight() -> dict:
    """List the required projects and source binding without starting a campaign."""
    try:
        binding = source_binding()
        binding_error = None
    except RuntimeError as error:
        binding, binding_error = None, str(error)
    return {
        "synthetic_only": True,
        "qualification": QUALIFICATION,
        "service_phase": SERVICE_PHASE,
        "ticket_reading_preview": ticket_reading_preview(),
        "check_id": CHECK_ID,
        "config": BETA_CONFIG,
        "config_present": (ROOT / BETA_CONFIG).is_file(),
        "projects": [{"project": name, **KIT.BETA_BROWSER_PROJECTS[name]} for name in PROJECTS],
        "required_specs": list(required_specs()),
        "source_binding": binding,
        "source_binding_error": binding_error,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        help="New matrix evidence directory (default: build-kit/evidence/beta-matrix-<id>)",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="List required projects and source binding; start no services",
    )
    parser.add_argument(
        "--ticket-reading-preview",
        action="store_true",
        help="Set PB_TICKET_READING_PREVIEW=1 for every campaign and require its vision suite",
    )
    args = parser.parse_args(argv)
    if args.ticket_reading_preview:
        os.environ["PB_TICKET_READING_PREVIEW"] = "1"
    if args.preflight:
        print(json.dumps(preflight(), indent=2))
        return 0
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        return run_matrix(args.out or ROOT / f"build-kit/evidence/beta-matrix-{time.time_ns()}")
    except RuntimeError as error:
        print(json.dumps({"error": str(error), "qualification": QUALIFICATION}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
