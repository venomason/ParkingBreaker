#!/usr/bin/env python3
"""Local kit integrity, readiness, check receipts and task scheduling; no provider calls."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tempfile
from urllib.parse import urlsplit

KIT = Path(__file__).resolve().parents[1]
REPO = KIT.parent
STATES = {"pending", "in_progress", "blocked", "accepted"}
CHECKS = {
    "kit-check": [sys.executable, "-m", "unittest", "discover", "-s", "build-kit/tests", "-v"],
    "backend-check": ["make", "check"],
    "web-lint": ["npm", "--prefix", "web", "run", "lint"],
    "web-types": ["npm", "--prefix", "web", "run", "type-check"],
    "web-build": ["npm", "--prefix", "web", "run", "build"],
    "web-browser": ["npm", "--prefix", "web", "run", "test:e2e", "--", "--reporter=json"],
    "synthetic-browser": ["npm", "--prefix", "web", "run", "test:e2e", "--",
        "tests/beta-acceptance.spec.ts", "tests/beta-phase-gate.spec.ts", "tests/beta-notices.spec.ts", "tests/configuration.spec.ts", "tests/cro-landing.spec.ts", "tests/customer-access-recovery.spec.ts", "tests/destination-choice.spec.ts",
        "tests/durable-destination.spec.ts", "tests/durable-intake.spec.ts",
        "tests/durable-letter.spec.ts", "tests/redesign.spec.ts", "tests/required-copies.spec.ts",
        "tests/fulfillment.spec.ts", "tests/ingress.spec.ts",
        "tests/launch1.spec.ts", "tests/live-presentation.spec.ts", "tests/pricing-assignment.spec.ts",
        "tests/runtime-phase.spec.ts",
        "tests/service-quality-browser.spec.ts", "tests/service-quality-states-browser.spec.ts",
        "tests/service-quality.spec.ts", "--reporter=json"],
    "synthetic-browser-controlled-free-beta": ["npm", "--prefix", "web", "run", "test:e2e", "--",
        "tests/beta-acceptance.spec.ts", "tests/beta-phase-gate.spec.ts",
        "tests/beta-notices.spec.ts", "tests/pricing-assignment.spec.ts",
        "tests/beta-full-journey.spec.ts", "--reporter=json"],
}


def read(path: Path):
    return json.loads(path.read_text())


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inside(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("Evidence and snapshot paths must be relative")
    result = (root / name).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("Path escapes kit")
    # Historical manifests keep their original names and exact hashes. Only
    # missing Markdown inside this checkout may resolve to its preserved archive.
    repo = REPO.resolve()
    if result.suffix.lower() == ".md" and not result.exists() and result.is_relative_to(repo):
        manifest = repo / "legacy/manifest.json"
        if manifest.is_file():
            if not manifest.resolve().is_relative_to(repo / "legacy"):
                raise ValueError("Archive manifest escapes repository")
            original = result.relative_to(repo).as_posix()
            matches = [row for row in read(manifest)["files"] if row["original"] == original]
            if len(matches) > 1:
                raise ValueError("Duplicate archived document")
            if matches:
                if matches[0]["archive"] != "legacy/" + original:
                    raise ValueError("Archived mapping must preserve its original path")
                archived = (repo / matches[0]["archive"]).resolve()
                archive_root = repo / "legacy" / root.resolve().relative_to(repo)
                if not archived.is_relative_to(archive_root):
                    raise ValueError("Archived document escapes original scope")
                if archived.is_file():
                    return archived
    return result


def validate_graph(tasks: list[dict]) -> list[str]:
    errors = []
    ids = [t.get("id") for t in tasks]
    if len(ids) != len(set(ids)):
        errors.append("Duplicate task ID")
    nodes = {t["id"]: t for t in tasks}
    visiting, seen = set(), set()

    def visit(node):
        if node in visiting:
            errors.append("Dependency cycle at " + node)
            return
        if node in seen:
            return
        visiting.add(node)
        for dep in nodes[node].get("depends_on", []):
            if dep not in nodes:
                errors.append(f"Missing dependency {dep} for {node}")
            else:
                visit(dep)
        visiting.remove(node)
        seen.add(node)

    for node in nodes:
        visit(node)
    for task in tasks:
        if not task.get("acceptance"):
            errors.append("Missing acceptance criteria: " + task["id"])
        if task["id"] in task.get("depends_on", []):
            errors.append("Task depends on itself: " + task["id"])
        for group in task.get("required_check_groups", []):
            if group not in CHECKS:
                errors.append("Unknown check group: " + group)
    return errors


def ready(tasks: list[dict], progress: dict) -> list[dict]:
    accepted = {tid for tid, s in progress.items() if s["status"] == "accepted"}
    return sorted(
        [t for t in tasks if progress[t["id"]]["status"] in {"pending", "in_progress"}
         and set(t["depends_on"]) <= accepted],
        key=lambda t: (progress[t["id"]]["status"] != "in_progress", t["priority"]),
    )


def verify_business_sources(root: Path) -> list[str]:
    """Check the declared review input, including each extracted package's own paths."""
    errors = []

    def check_manifest(manifest: Path, base: Path):
        try:
            document = read(manifest)
            entries = document["files"]
            if not isinstance(entries, list) or not entries:
                raise ValueError("files must be a nonempty list")
            seen = set()
            for entry in entries:
                name = entry["path"]
                path = inside(base, name)
                if path in seen:
                    errors.append("Duplicate source entry: " + name)
                seen.add(path)
                if not path.is_file():
                    errors.append("Missing business/component source: " + name)
                    continue
                data = path.read_bytes()
                if digest(data) != entry["sha256"]:
                    errors.append("Changed business/component source: " + name)
                if "bytes" in entry and len(data) != entry["bytes"]:
                    errors.append("Source size mismatch: " + name)
            return document
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"Invalid source manifest {manifest.name}: {exc}")
            return None

    document = check_manifest(root / "business-source-manifest.json", root.parent)
    if document is not None:
        components = document.get("selected_extracted_components")
        if not isinstance(components, list) or not components:
            errors.append("Missing selected extracted component manifests")
        else:
            for name in components:
                try:
                    manifest = inside(root, name)
                    check_manifest(manifest, manifest.parent)
                except (ValueError, TypeError) as exc:
                    errors.append(f"Invalid component manifest path: {exc}")
    return errors


def verify_archive(repo: Path = REPO) -> list[str]:
    """Check archived originals without requiring the owner's untracked files in clones."""
    errors, seen = [], set()
    try:
        archive = repo / "legacy"
        manifest = archive / "manifest.json"
        if not manifest.resolve().is_relative_to(repo.resolve() / "legacy"):
            return ["Archive manifest escapes repository"]
        for row in read(manifest)["files"]:
            name = row["original"]
            if name in seen or row["archive"] != "legacy/" + name:
                errors.append("Invalid or duplicate archive entry: " + name)
                continue
            seen.add(name)
            path = inside(archive, name)
            if not path.is_file():
                if row["tracked_before"]:
                    errors.append("Missing archived original: " + name)
                continue
            raw = path.read_bytes()
            if digest(raw) != row["sha256"] or len(raw) != row["bytes"]:
                errors.append("Changed archived original: " + name)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append("Invalid archive: " + str(exc))
    return errors


def verify(root: Path = KIT) -> list[str]:
    errors = []
    if root.resolve() == KIT.resolve():
        errors.extend(verify_archive())
    errors.extend(verify_business_sources(root))
    tasks = read(root / "tasks.json")["tasks"]
    errors.extend(validate_graph(tasks))
    progress = read(root / "state/progress.json")["tasks"]
    ids = {t["id"] for t in tasks}
    if ids != set(progress):
        errors.append("Task ledger and graph IDs differ")
    required = ["START-HERE.md", "PRODUCT.md", "DECISIONS.md", "JURISDICTIONS.md",
                "ADDRESS-RESPONSIBILITY.md", "TRANSACTION.md", "PROVIDERS.md", "WEB.md",
                "PRIVACY.md", "ACCEPTANCE.md", "OPERATIONS.md", "PREREQUISITES.md",
                "CONTINUITY.md", "KICKOFF.md", "CURRENT-FLOW-ANALYSIS.md",
                "legacy-audit/DISSECTION.md", "BUSINESS-AUTOMATION.md",
                "business-automation.json", "CUSTOMER-DATA-RETENTION.md",
                "SERVICE-QUALITY.md", "recipes/07-telemetry-business-ops.md"]
    for name in required + [t["spec"] for t in tasks]:
        if not inside(root, name).is_file():
            errors.append("Missing document: " + name)
    for tid, state in progress.items():
        if state.get("status") not in STATES:
            errors.append("Invalid state: " + tid)
        if state.get("status") == "accepted":
            receipt = state.get("receipt")
            if not receipt or not inside(root, receipt).is_file():
                errors.append("Accepted task has no receipt: " + tid)
    for source in read(root / "sources/manifest.json"):
        p = inside(root, source["snapshot"])
        if not p.is_file() or digest(p.read_bytes()) != source["sha256"]:
            errors.append("Changed/missing source snapshot: " + source["snapshot"])
    findings = read(root / "audit-findings.json")["findings"]
    fids = [f["id"] for f in findings]
    if len(fids) != len(set(fids)):
        errors.append("Duplicate audit finding ID")
    for finding in findings:
        if finding["owner_task"] not in ids or not inside(root, finding["source"]).is_file():
            errors.append("Orphan finding: " + finding["id"])
    cases = read(root / "fixtures/jurisdiction-cases.json")["cases"]
    if len({c["id"] for c in cases}) != len(cases):
        errors.append("Duplicate scenario ID")
    for case in cases:
        if not all(case.get(x) for x in ("expected_decision", "protects", "fixture_kind")):
            errors.append("Incomplete scenario: " + case["id"])
    return errors


def application_fingerprint(repo: Path = REPO) -> str:
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--",
         "backend", "web", "deploy", "legacy/backend", "legacy/deploy",
         "Makefile", "compose.yaml"], cwd=repo,
    ).split(b"\0")
    parts = []
    for raw in sorted(set(paths)):
        if not raw:
            continue
        rel = raw.decode()
        if Path(rel).name.startswith(".env"):
            continue
        p = repo / rel
        if p.is_file() and not p.is_symlink():
            parts.append(raw + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return digest(b"\n".join(parts))


def env_presence(repo: Path = REPO) -> dict[str, bool]:
    names = {key: bool(value.strip()) for key, value in os.environ.items()}
    file = repo / ".env"
    if file.is_file():
        for line in file.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in os.environ:
                names[key] = bool(value.strip().strip("\"'"))
    return names


def preflight(root: Path = KIT, repo: Path = REPO) -> dict:
    presence = env_presence(repo)
    items = read(root / "prerequisites.json")["items"]
    results = []
    for item in items:
        record = {"id": item["id"], "title": item["title"], "stage": item["stage"],
                  "credential_presence": {n: presence.get(n, False) for n in item["presence_names"]},
                  "verified": False}
        evidence = item.get("evidence")
        if item.get("status") == "verified" and isinstance(evidence, dict):
            p = inside(root, evidence.get("path", ""))
            record["verified"] = p.is_file() and digest(p.read_bytes()) == evidence.get("sha256")
        results.append(record)
    tools = {name: bool(shutil.which(name)) for name in ("python3", "git", "uv", "node", "npm", "docker", "gcloud")}
    progress = read(root / "state/progress.json")["tasks"]
    external_ready = all(x["verified"] for x in results)
    accepted = all(s["status"] == "accepted" for s in progress.values())
    return {"tools": tools, "engineering_tools_present": all(tools[n] for n in ("python3", "git", "uv", "node", "npm")),
            "external_prerequisites": results, "external_prerequisites_verified": external_ready,
            "all_application_tasks_accepted": accepted, "launch_ready": external_ready and accepted,
            "note": "Presence is checked only in this process and the workspace .env. An absent value does not mean an existing credential/account is unavailable elsewhere. Reuse authorized existing secure configuration before requesting setup. Presence alone does not prove authentication, account capability or spending authority."}


SYNTHETIC_BROWSER_CHECKS = {"synthetic-browser", "synthetic-browser-controlled-free-beta"}
BROWSER_CHECKS = {"web-browser", *SYNTHETIC_BROWSER_CHECKS}
TICKET_READING_SPEC = "tests/beta-ticket-vision.spec.ts"
# The controlled-free beta matrix runs one explicit project per owned campaign.
# Mobile entries emulate a layout viewport only; they are not device or camera
# qualification.
BETA_BROWSER_CONFIG = "playwright.beta.config.ts"
BETA_BROWSER_PROJECTS = {
    "chromium-desktop1280x900": {"engine": "chromium", "viewport": [1280, 900]},
    "chromium-mobile375x812": {"engine": "chromium", "viewport": [375, 812]},
    "firefox-desktop1280x900": {"engine": "firefox", "viewport": [1280, 900]},
    "firefox-mobile375x812": {"engine": "firefox", "viewport": [375, 812]},
}


def synthetic_browser_check(service_phase: str = "paid") -> str:
    """Keep historical paid receipts stable; select an explicit beta suite."""
    if service_phase == "paid":
        return "synthetic-browser"
    if service_phase == "controlled_free_beta":
        return "synthetic-browser-controlled-free-beta"
    raise ValueError("Unknown synthetic service phase")


def browser_check_command(name: str, ticket_reading_preview: bool = False) -> list[str]:
    """Keep default campaigns stable; preview explicitly requires its vision suite."""
    if type(ticket_reading_preview) is not bool:
        raise ValueError("Ticket-reading preview must be a boolean")
    command = list(CHECKS[name])
    if ticket_reading_preview:
        if name not in SYNTHETIC_BROWSER_CHECKS:
            raise ValueError("Ticket-reading preview requires a synthetic browser check")
        command.insert(-1, TICKET_READING_SPEC)
    return command


def beta_browser_check_command(project: str, ticket_reading_preview: bool = False) -> list[str]:
    """Bind one required beta project to the canonical controlled-free suites."""
    if project not in BETA_BROWSER_PROJECTS:
        raise ValueError("Unknown beta browser project")
    command = browser_check_command("synthetic-browser-controlled-free-beta", ticket_reading_preview)
    return [*command[:-1], f"--config={BETA_BROWSER_CONFIG}", f"--project={project}", command[-1]]


def beta_transport_valid(env: dict, project: str) -> bool:
    """A selected project must carry its own explicit owned transport, no fallback."""
    if project not in BETA_BROWSER_PROJECTS:
        return False
    mapped = re.fullmatch(
        r"MAP portal\.example\.test 127\.0\.0\.1:([1-9]\d{0,4})",
        env.get("PLAYWRIGHT_HOST_RULES", ""),
    )
    if not mapped or int(mapped.group(1)) > 65535:
        return False
    try:
        proxy = urlsplit(env.get("PLAYWRIGHT_FIREFOX_PROXY", ""))
        proxy_port = proxy.port
    except ValueError:
        return False
    chrome = env.get("PLAYWRIGHT_CHROME_PATH", "").strip()
    return (
        proxy.scheme == "http"
        and proxy.hostname in {"127.0.0.1", "localhost", "::1"}
        and proxy.username is None
        and proxy.password is None
        and not proxy.path
        and not proxy.query
        and not proxy.fragment
        and proxy_port is not None
        and bool(chrome)
        and Path(chrome).is_file()
    )


def browser_report_validation(path: Path, required_specs=None, project=None) -> dict:
    """Validate observed Playwright JSON, counting tests through nested suites."""
    try:
        report = read(path)
        if not isinstance(report, dict) or report.get("errors") != []:
            raise ValueError("report-level errors or missing errors inventory")
        counts = {"expected": 0, "unexpected": 0, "flaky": 0, "skipped": 0}
        results = 0
        observed_specs = set()
        observed_projects = set()

        def suites(items, inherited_file=None):
            nonlocal results
            if not isinstance(items, list):
                raise ValueError("invalid suites")
            for suite in items:
                if not isinstance(suite, dict):
                    raise ValueError("invalid suite")
                suite_file = suite.get("file", inherited_file)
                specs = suite.get("specs", [])
                if not isinstance(specs, list):
                    raise ValueError("invalid specs")
                for spec in specs:
                    if not isinstance(spec, dict) or not isinstance(spec.get("tests"), list) or not spec["tests"]:
                        raise ValueError("spec has no tests")
                    for test in spec["tests"]:
                        if not isinstance(test, dict) or test.get("status") != "expected" or test.get("expectedStatus") != "passed":
                            raise ValueError("non-passing test")
                        attempts = test.get("results")
                        if not isinstance(attempts, list) or not attempts:
                            raise ValueError("test has no results")
                        for result in attempts:
                            if not isinstance(result, dict) or result.get("status") != "passed" or result.get("errors", []) or result.get("error"):
                                raise ValueError("non-passing result")
                        results += len(attempts)
                        counts["expected"] += 1
                        file = spec.get("file", suite_file)
                        if isinstance(file, str):
                            observed_specs.add(Path(file).name)
                        if project is not None:
                            observed_projects.add(test.get("projectName", test.get("projectId")))
                suites(suite.get("suites", []), suite_file)

        suites(report.get("suites"))
        stats = report.get("stats")
        if not counts["expected"] or not isinstance(stats, dict):
            raise ValueError("empty report")
        if any(type(stats.get(k)) is not int or stats[k] != v for k, v in counts.items()):
            raise ValueError("stats disagree with observed tests")
        if required_specs and not {Path(file).name for file in required_specs} <= observed_specs:
            raise ValueError("required spec has no observed passing test")
        if project is not None and (not observed_projects or observed_projects != {project}):
            raise ValueError("report was not produced by exactly the required project")
        return {"valid": True, "tests": counts["expected"], "results": results}
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return {"valid": False, "reason": "Missing or invalid complete passing Playwright report"}


def synthetic_environment_valid(env: dict) -> bool:
    def target(name, scheme, hosts=None):
        try:
            url = urlsplit(env.get(name, ""))
            return (url.scheme == scheme and bool(url.hostname) and url.username is None
                    and url.password is None and not url.path and not url.query and not url.fragment
                    and (url.hostname in hosts if hosts else url.hostname.endswith(".example.test"))
                    and (url.port is None or 0 < url.port < 65536))
        except ValueError:
            return False
    loopback = {"localhost", "127.0.0.1", "::1"}
    return (env.get("SYNTHETIC_REHEARSAL") == "1"
            and target("SECURE_BASE", "https")
            and env.get("SYNTHETIC_BASE_URL") == env.get("SECURE_BASE")
            and env.get("DEST_BASE") == env.get("SECURE_BASE")
            and target("INGRESS_API_BASE", "http", loopback)
            and target("SECURE_TRANSPORT_BASE", "https", loopback)
            and target("PRODUCTION_BASE", "http", loopback)
            and all(env.get(key, "").strip() for key in (
                "INGRESS_WORKER_SECRET", "T37_LOB_WEBHOOK_SECRET", "SYNTHETIC_CLOCK_PATH", "T37_DATA_DIR")))


def run_check(name: str, project: str | None = None) -> Path:
    if name == "web-browser":
        url = urlsplit(os.environ.get("LIVE_BASE", ""))
        if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Browser acceptance needs LIVE_BASE pointing to the actual local rehearsal; skipped tests are not acceptance")
    ticket_reading_preview = name in SYNTHETIC_BROWSER_CHECKS and os.environ.get("PB_TICKET_READING_PREVIEW") == "1"
    command = browser_check_command(name, ticket_reading_preview)
    if name in SYNTHETIC_BROWSER_CHECKS:
        if ticket_reading_preview and not (REPO / "web" / TICKET_READING_SPEC).is_file():
            raise ValueError("Ticket-reading preview requires " + TICKET_READING_SPEC)
        if not synthetic_environment_valid(dict(os.environ)):
            raise ValueError("Synthetic browser check requires explicit local fixture environment")
        if name != synthetic_browser_check(os.environ.get("PB_SERVICE_PHASE", "paid")):
            raise ValueError("Synthetic browser check does not match the service phase")
        if project is not None:
            if name != "synthetic-browser-controlled-free-beta":
                raise ValueError("Only the controlled-free beta check selects a project")
            if not beta_transport_valid(dict(os.environ), project):
                raise ValueError("Beta project needs its owned transport; never substitute")
            command = beta_browser_check_command(project, ticket_reading_preview)
    before = application_fingerprint()
    started = time.time()
    ident = f"{time.time_ns()}-{name}"
    directory = KIT / "evidence/checks"
    directory.mkdir(parents=True, exist_ok=True)
    browser = {}
    with tempfile.TemporaryDirectory(prefix="pb-browser-report-") as temporary:
        env = dict(os.environ)
        report = Path(temporary) / "playwright.json"
        if name in BROWSER_CHECKS:
            env["PLAYWRIGHT_JSON_OUTPUT_FILE"] = str(report)
        result = subprocess.run(command, cwd=REPO, capture_output=True, text=True, check=False, **({"env": env} if name in BROWSER_CHECKS else {}))
        if name in BROWSER_CHECKS:
            validation = browser_report_validation(report, required_browser_specs(name, ticket_reading_preview), project)
            browser = {"browser_report_validation": validation}
            if name in SYNTHETIC_BROWSER_CHECKS:
                browser["ticket_reading_preview"] = ticket_reading_preview
            if project is not None:
                browser["browser_project"] = project
            if report.is_file():
                saved = directory / (ident + ".playwright.json")
                shutil.copyfile(report, saved)
                browser.update(browser_report=str(saved.relative_to(KIT)), browser_report_sha256=digest(saved.read_bytes()))
    output = result.stdout + result.stderr
    for key, value in os.environ.items():
        if any(part in key for part in ("SECRET", "TOKEN", "API_KEY", "PASSWORD")) and len(value) >= 8:
            output = output.replace(value, "[REDACTED]")
    log = directory / (ident + ".log")
    log.write_text(output)
    receipt = directory / (ident + ".json")
    exit_code = result.returncode
    if name in BROWSER_CHECKS and not browser["browser_report_validation"]["valid"] and exit_code == 0:
        exit_code = 1
    receipt.write_text(json.dumps({"check_id": name, "command": command, "cwd": ".",
        "exit_code": exit_code, "command_exit_code": result.returncode,
        "started_unix": started, "duration_seconds": time.time() - started,
        "application_fingerprint": before, "code_unchanged_during_check": before == application_fingerprint(),
        "log": str(log.relative_to(KIT)), "log_sha256": digest(log.read_bytes()), "observed": True,
        **browser}, indent=2) + "\n")
    return receipt


def required_browser_specs(name: str, ticket_reading_preview: bool = False) -> set[str]:
    return {entry for entry in browser_check_command(name, ticket_reading_preview) if entry.endswith(".spec.ts")} if name in SYNTHETIC_BROWSER_CHECKS else set()


def browser_receipt_valid(check: dict, root: Path) -> bool:
    if check.get("check_id") not in BROWSER_CHECKS:
        return True
    try:
        report = inside(root, check.get("browser_report", ""))
        return (
            report.is_file()
            and digest(report.read_bytes()) == check.get("browser_report_sha256")
            and browser_report_validation(
                report,
                required_browser_specs(check["check_id"], check.get("ticket_reading_preview", False)),
                check.get("browser_project"),
            )["valid"]
        )
    except (OSError, ValueError, TypeError):
        return False


def expected_check_command(check: dict):
    """A project receipt binds its own explicit command; every other check stays canonical."""
    project = check.get("browser_project")
    try:
        preview = check.get("ticket_reading_preview", False)
        if project is None:
            return browser_check_command(check.get("check_id"), preview)
        return beta_browser_check_command(project, preview)
    except (ValueError, KeyError):
        return None


def validate_receipt(task: dict, receipt: dict, root: Path, fingerprint: str) -> list[str]:
    errors = []
    if receipt.get("task_id") != task["id"] or not all(receipt.get(f) for f in ("reviewer", "reviewed_diff", "conclusion")):
        errors.append("Receipt needs matching task, reviewer, reviewed diff and conclusion")
    proof = receipt.get("criteria_evidence", [])
    if len(proof) != len(task["acceptance"]) or {p.get("criterion") for p in proof} != set(range(1, len(task["acceptance"]) + 1)):
        errors.append("Every acceptance criterion needs evidence")
    for entry in proof:
        if not entry.get("paths"):
            errors.append("Criterion has no concrete evidence files")
        for path in entry.get("paths", []):
            if not inside(root, path).is_file():
                errors.append("Missing criterion evidence: " + path)
    good = set()
    for path in receipt.get("check_receipts", []):
        check = read(inside(root, path))
        log = inside(root, check["log"])
        if (check.get("observed") is True and check.get("exit_code") == 0
                and check.get("code_unchanged_during_check") is True
                and check.get("application_fingerprint") == fingerprint
                and log.is_file() and digest(log.read_bytes()) == check.get("log_sha256")
                and check.get("command") == expected_check_command(check)
                and browser_receipt_valid(check, root)):
            good.add(check["check_id"])
    if not set(task["required_check_groups"]) <= good:
        errors.append("Missing current successful observed check receipts")
    if task["kind"] == "external_acceptance" and not receipt.get("external_evidence"):
        errors.append("External acceptance requires actual provider/environment evidence")
    for path in receipt.get("external_evidence", []):
        if not inside(root, path).is_file():
            errors.append("Missing external evidence: " + path)
    return errors


# Owner directive 2026-10-27: hashes of the committed T21-TRIGGERS guard baseline.
# guard-freeze.json may differ from these only with a valid owner_approved_exception.
GUARD_BASELINE = {
    "backend/src/parkingbreaker/research/support.py": "76f32a8f569fd0561f3c84fe9060abbedffec3b3dc1403c0380e182f21106852",
    "backend/src/parkingbreaker/research/findings.py": "9a1d84aa9a7eea69e21964a19ea61be2efdfcd43026e34728b415bb43d0169f1",
    "backend/src/parkingbreaker/research/procedure.py": "580e52a8793bc53e48aedfbc9257a4fbabe8ddd3305b300ae3f41df0494af190",
    "backend/src/parkingbreaker/research/route_requirements.py": "c5a1b3a026c1f585c989b773412103d747a746ed53652c63c2302f34b74bd279",
}
EXCEPTION_FIELDS = ("approved_by", "approved_on", "case_id", "captured_url", "captured_page_sha256")


def guard_exception_errors(exception, root: Path = KIT) -> list[str]:
    if not isinstance(exception, dict) or set(exception) != set(EXCEPTION_FIELDS):
        return ["Guard freeze exception needs exactly: " + ", ".join(EXCEPTION_FIELDS)]
    if not all(isinstance(exception[f], str) and exception[f].strip() for f in EXCEPTION_FIELDS):
        return ["Guard freeze exception fields must be nonempty text"]
    errors = []
    if "owner" not in exception["approved_by"].lower():
        errors.append("Guard freeze exception must be approved by the owner")
    try:
        time.strptime(exception["approved_on"], "%Y-%m-%d")
    except ValueError:
        errors.append("Guard freeze exception approved_on must be YYYY-MM-DD")
    if not exception["captured_url"].startswith("https://"):
        errors.append("Guard freeze exception captured_url must be https")
    sha = exception["captured_page_sha256"]
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        errors.append("Guard freeze exception captured_page_sha256 must be 64 lowercase hex")
        return errors
    pages = root / "evidence/bench-pages"
    raw, text = pages / sha, pages / (sha + ".txt")
    # save_page names extracted text by the original body hash; a raw body must match it.
    if not (text.is_file() or (raw.is_file() and digest(raw.read_bytes()) == sha)):
        errors.append("Guard freeze exception captured page is not saved under evidence/bench-pages")
    return errors


def guard_freeze_errors(root: Path = KIT, repo: Path = REPO) -> list[str]:
    path = root / "state/guard-freeze.json"
    if not path.is_file():
        return ["Missing guard freeze: state/guard-freeze.json"]
    try:
        freeze = read(path)
    except json.JSONDecodeError:
        return ["Guard freeze file is not valid JSON"]
    files, exception = freeze.get("files"), freeze.get("owner_approved_exception")
    if not isinstance(files, dict) or set(files) != set(GUARD_BASELINE):
        return ["Guard freeze must list exactly the four frozen research guard files"]
    errors = []
    if files != GUARD_BASELINE or exception is not None:
        errors.extend(guard_exception_errors(exception, root))
    for name, expected in sorted(files.items()):
        target = repo / name
        if not target.is_file() or digest(target.read_bytes()) != expected:
            errors.append("Frozen research guard changed without owner approval: " + name)
    return errors


def doctor() -> dict:
    """Read-only workspace checks, using the existing verification and journal paths."""
    errors = verify()
    errors.extend(guard_freeze_errors())
    spec = importlib.util.spec_from_file_location("parkingbreaker_checkpoint", KIT / "scripts/checkpoint.py")
    checkpoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checkpoint)
    errors.extend(checkpoint.status_errors())
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = REPO / name
        if not path.is_symlink() or path.readlink() != Path("STATUS.md"):
            errors.append(name + " must point to the single living STATUS.md")
    commands = {name: bool(shutil.which(name)) for name in ("python3", "git", "uv", "node", "npm")}
    errors.extend("Missing development command: " + name for name, present in commands.items() if not present)
    return {"workspace_ready": not errors, "errors": errors, "commands": commands,
            "living_status": "STATUS.md", "launch_acceptance": "separate and unchanged"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["doctor", "verify", "status", "next", "preflight", "run-check", "accept"])
    parser.add_argument("value", nargs="?")
    parser.add_argument(
        "--project",
        choices=tuple(BETA_BROWSER_PROJECTS),
        help="Run one explicit controlled-free beta browser project",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor()
            print(json.dumps(result, indent=2))
            return int(not result["workspace_ready"])
        if args.command == "verify":
            errors = verify()
            print(json.dumps({"kit_valid": not errors, "errors": errors, "application_acceptance": "not established by kit verification"}, indent=2))
            return bool(errors)
        if args.command == "preflight":
            print(json.dumps(preflight(), indent=2))
            return 0
        if args.command == "run-check":
            if args.value not in CHECKS:
                raise ValueError("Choose a defined check ID")
            receipt = run_check(args.value, args.project)
            print(json.dumps({"receipt": str(receipt.relative_to(KIT)), "exit_code": read(receipt)["exit_code"]}))
            return int(read(receipt)["exit_code"] != 0)
        tasks = read(KIT / "tasks.json")["tasks"]
        ledger = read(KIT / "state/progress.json")
        if args.command in {"status", "next"}:
            choices = ready(tasks, ledger["tasks"])
            print(json.dumps({"next": [{"id": t["id"], "title": t["title"], "spec": t["spec"]} for t in choices],
                "accepted": sum(s["status"] == "accepted" for s in ledger["tasks"].values()), "total": len(tasks)}, indent=2))
        elif args.command == "accept":
            if not args.value:
                raise ValueError("accept needs an evidence receipt path relative to the kit")
            receipt = read(inside(KIT, args.value))
            task = next(t for t in tasks if t["id"] == receipt["task_id"])
            hold = ledger["tasks"][task["id"]].get("review_hold")
            if hold and hold.get("acceptance_allowed") is not True:
                raise ValueError("Task acceptance is held: " + hold.get("reason", "final review required"))
            if any(ledger["tasks"][dep]["status"] != "accepted" for dep in task["depends_on"]):
                raise ValueError("Dependencies are not accepted")
            errors = validate_receipt(task, receipt, KIT, application_fingerprint())
            if errors:
                raise ValueError("; ".join(errors))
            ledger["tasks"][task["id"]] = {"status": "accepted", "receipt": args.value, "blocked_by": None}
            temp = KIT / "state/progress.json.tmp"
            temp.write_text(json.dumps(ledger, indent=2) + "\n")
            temp.replace(KIT / "state/progress.json")
            print(json.dumps({"accepted": task["id"], "note": "Supervisor owns semantic acceptance."}))
        return 0
    except (ValueError, KeyError, OSError, StopIteration, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
