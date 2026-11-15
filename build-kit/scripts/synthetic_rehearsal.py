#!/usr/bin/env python3
"""Build and rehearse the actual web app with fresh, loopback-only fake providers.

Run from any directory. Dependencies must already be installed; this command never
installs packages, reads provider credentials, deploys, or enables external services.
Each invocation keeps its private synthetic runtime and a fresh evidence directory.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.util
import json
import os
import secrets
import select
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://portal.example.test"
MARKER = "ParkingBreaker synthetic rehearsal\n"
# Firefox has no Chromium host-resolver flag, so one bounded loopback CONNECT
# proxy tunnels only this canonical authority to the owned loopback TLS port.
CONNECT_AUTHORITY = ("portal.example.test", 443)
TUNNEL_IDLE_SECONDS = 120
HOP_HEADERS = {
    "connection",
    "transfer-encoding",
    "keep-alive",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
}


def clean_environment() -> dict[str, str]:
    """Allow system settings and the explicit preview toggle, never provider settings."""
    return {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "SYSTEMROOT", "PB_TICKET_READING_PREVIEW")
        if key in os.environ
    }


def kit_module():
    spec = importlib.util.spec_from_file_location(
        "rehearsal_kit", ROOT / "build-kit/scripts/kit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prerequisites() -> dict[str, str]:
    # Next loads these files independently of subprocess environment filtering.
    if any(
        (ROOT / "web" / name).exists()
        for name in (".env", ".env.local", ".env.production", ".env.production.local")
    ):
        raise RuntimeError(
            "Synthetic build requires a clean checkout without web .env files"
        )
    tools = {
        name: shutil.which(name) for name in ("node", "npm", "openssl", "pdftoppm")
    }
    tools["python"] = str(ROOT / "backend/.venv/bin/python")
    tools["next"] = str(ROOT / "web/node_modules/next/dist/bin/next")
    tools["chrome"] = (
        shutil.which("google-chrome")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
    )
    missing = [
        name for name, path in tools.items() if not path or not Path(path).is_file()
    ]
    if missing:
        raise RuntimeError(
            "Missing installed rehearsal dependencies: " + ", ".join(missing)
        )
    return tools


def observed_browser_receipt_valid(
    kit,
    receipt: dict,
    fingerprint: str,
    service_phase: str = "paid",
    project: str | None = None,
    ticket_reading_preview: bool = False,
) -> bool:
    """Independently bind the observed command, source, report and raw output."""
    try:
        check_id = kit.synthetic_browser_check(service_phase)
        command = (
            kit.beta_browser_check_command(project, ticket_reading_preview)
            if project is not None else kit.browser_check_command(check_id, ticket_reading_preview)
        )
        log = kit.inside(ROOT / "build-kit", receipt["log"])
        return (
            receipt.get("check_id") == check_id
            and receipt.get("command") == command
            and receipt.get("browser_project") == project
            and receipt.get("ticket_reading_preview", False) is ticket_reading_preview
            and receipt.get("observed") is True
            and receipt.get("exit_code") == receipt.get("command_exit_code") == 0
            and receipt.get("code_unchanged_during_check") is True
            and receipt.get("application_fingerprint") == fingerprint
            and log.is_file()
            and hashlib.sha256(log.read_bytes()).hexdigest()
            == receipt.get("log_sha256")
            and kit.browser_receipt_valid(receipt, ROOT / "build-kit")
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def selected_project(kit, project: str | None, service_phase: str) -> str | None:
    """One explicit beta project only; never a default, skip or substitute."""
    if project is None:
        return None
    if service_phase != "controlled_free_beta":
        raise ValueError("Beta browser projects require the controlled_free_beta phase")
    if project not in kit.BETA_BROWSER_PROJECTS:
        raise ValueError("Unknown beta browser project")
    return project


def connect_destination_allowed(host: str, port: int) -> bool:
    """Only the canonical HTTPS authority may be tunneled; nothing is generalised."""
    return (host, port) == CONNECT_AUTHORITY


def ports(count: int = 3) -> tuple[int, ...]:
    reserved = [socket.socket() for _ in range(count)]
    try:
        for sock in reserved:
            sock.bind(("127.0.0.1", 0))
        return tuple(sock.getsockname()[1] for sock in reserved)
    finally:
        for sock in reserved:
            sock.close()


def prepare_web(runtime: Path, output: Path) -> Path:
    """Build in a private source copy so an existing development server stays intact."""
    web = runtime / "web"
    web.mkdir()
    names = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "web",
        ],
        cwd=ROOT,
    ).split(b"\0")
    manifest = {}
    for raw in sorted(set(names)):
        if not raw:
            continue
        relative = Path(raw.decode())
        if relative.name.startswith(".env"):
            continue
        source = ROOT / relative
        if source.is_symlink() or not source.is_file():
            raise RuntimeError("Synthetic web source must contain only regular files")
        target = runtime / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        manifest[relative.as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
        if (
            manifest[relative.as_posix()]
            != hashlib.sha256(source.read_bytes()).hexdigest()
        ):
            raise RuntimeError("Web source changed during synthetic preparation")
    (web / "node_modules").symlink_to(
        (ROOT / "web/node_modules").resolve(), target_is_directory=True
    )
    (output / "web-source.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return web


class OwnedProcesses:
    def __init__(self) -> None:
        self.children: list[subprocess.Popen] = []
        self.logs = []
        self.released: set[int] = set()
        self.stopped: dict[int, bool] = {}

    def launch(
        self, command: list[str], env: dict[str, str], log: Path, *, cwd: Path = ROOT
    ):
        handle = log.open("w")
        self.logs.append(handle)
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.children.append(child)
        return child

    def run(
        self,
        command: list[str],
        env: dict[str, str],
        log: Path,
        timeout: int,
        *,
        cwd: Path = ROOT,
    ) -> None:
        child = self.launch(command, env, log, cwd=cwd)
        try:
            if child.wait(timeout=timeout) != 0:
                raise RuntimeError("Command failed; inspect " + str(log))
        finally:
            self.stop_group(child)

    @staticmethod
    def live_group(group: int) -> bool:
        # Linux /proc distinguishes exited zombies from running group members.
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == group and fields[0] not in {"Z", "X"}:
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False

    def stop_group(self, child: subprocess.Popen) -> bool:
        if child.pid in self.released:
            return True
        # The process group can outlive its leader (npm and browser grandchildren).
        for signum, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 3)):
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass
            until = time.monotonic() + grace
            while self.live_group(child.pid) and time.monotonic() < until:
                time.sleep(0.05)
            if not self.live_group(child.pid):
                child.wait(timeout=1)
                self.released.add(child.pid)
                return True
        return False

    def close(self) -> bool:
        stopped = True
        for child in reversed(self.children):
            child_stopped = self.stop_group(child)
            self.stopped[child.pid] = child_stopped
            stopped = child_stopped and stopped
        for handle in self.logs:
            handle.close()
        return stopped


def wait_fake(url: str, children: list[subprocess.Popen], timeout: int = 60) -> None:
    opener = build_opener(
        ProxyHandler({}), HTTPSHandler(context=ssl._create_unverified_context())
    )
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if any(child.poll() is not None for child in children):
            raise RuntimeError("An owned rehearsal service exited during startup")
        try:
            with opener.open(url, timeout=2) as response:
                if json.load(response).get("mode") == "fake":
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    raise RuntimeError("Timed out waiting for the owned fake-provider rehearsal")


def wait_tcp(port: int, children: list[subprocess.Popen], timeout: int = 30) -> None:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if any(child.poll() is not None for child in children):
            raise RuntimeError("An owned rehearsal service exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("Timed out waiting for the owned loopback CONNECT proxy")


def connect_proxy_server(proxy_port: int, tls_port: int) -> ThreadingHTTPServer:
    """Build, but do not start, the bounded loopback CONNECT proxy.

    Only portal.example.test:443 may be tunneled, and only to the owned loopback
    TLS port. Firefox cannot use Chromium's --host-resolver-rules flag, so this
    proxy preserves the canonical HTTPS origin for it; any other authority, plain
    HTTP proxy request or non-loopback upstream is refused.
    """

    class Tunnel(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:
            pass

        def record(self, event: str, authority: str) -> None:
            print(json.dumps({"connect": event, "authority": authority[:128]}), flush=True)

        def refuse(self) -> None:
            self.record("refused", self.command + " " + self.path)
            self.close_connection = True
            self.send_error(405, "Only bounded CONNECT is allowed")

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse

        def do_CONNECT(self) -> None:
            authority = self.path
            host, _, raw_port = authority.rpartition(":")
            try:
                port = int(raw_port)
            except ValueError:
                port = 0
            if not host or not connect_destination_allowed(host, port):
                self.record("refused", authority)
                self.close_connection = True
                self.send_error(403, "Destination not allowed")
                return
            try:
                upstream = socket.create_connection(("127.0.0.1", tls_port), timeout=10)
            except OSError:
                self.record("unavailable", authority)
                self.close_connection = True
                self.send_error(502, "Owned transport unavailable")
                return
            self.record("allowed", authority)
            self.send_response(200, "Connection Established")
            self.end_headers()
            self.wfile.flush()
            try:
                self.tunnel(self.connection, upstream)
            finally:
                upstream.close()
                self.close_connection = True

        @staticmethod
        def tunnel(downstream: socket.socket, upstream: socket.socket) -> None:
            peers = {downstream: upstream, upstream: downstream}
            try:
                while True:
                    readable, _, _ = select.select(list(peers), [], [], TUNNEL_IDLE_SECONDS)
                    if not readable:
                        return
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        peers[source].sendall(data)
            except OSError:
                return

    server = ThreadingHTTPServer(("127.0.0.1", proxy_port), Tunnel)
    server.daemon_threads = True
    return server


def connect_proxy(proxy_port: int, tls_port: int) -> None:
    """Serve the owned CONNECT proxy until the reaping rehearsal stops it."""
    connect_proxy_server(proxy_port, tls_port).serve_forever()


def proxy(runtime: Path, web_port: int, tls_port: int) -> None:
    """Forward through the real Next ingress; never shortcut /api to private API."""
    if runtime.is_symlink() or (runtime / "SYNTHETIC_ONLY").read_text() != MARKER:
        raise RuntimeError("Invalid synthetic proxy runtime")

    class Forward(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def forward(self) -> None:
            if not self.path.startswith("/") or self.headers.get("Transfer-Encoding"):
                self.send_error(400)
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in HOP_HEADERS
            }
            headers["Host"] = "portal.example.test"
            connection = http.client.HTTPConnection("127.0.0.1", web_port, timeout=90)
            try:
                connection.request(self.command, self.path, body=body, headers=headers)
                response = connection.getresponse()
                data = response.read()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in HOP_HEADERS | {"content-length"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
            except (OSError, http.client.HTTPException):
                self.send_error(502)
            finally:
                connection.close()

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = (
            forward
        )

    server = ThreadingHTTPServer(("127.0.0.1", tls_port), Forward)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(runtime / "cert.pem", runtime / "key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def rehearse(output: Path, service_phase: str = "paid", project: str | None = None) -> int:
    if service_phase not in {"paid", "controlled_free_beta"}:
        raise ValueError("Unknown synthetic service phase")
    kit = kit_module()
    project = selected_project(kit, project, service_phase)
    ticket_reading_preview = os.environ.get("PB_TICKET_READING_PREVIEW") == "1"
    if ticket_reading_preview and not (ROOT / "web" / kit.TICKET_READING_SPEC).is_file():
        raise ValueError("Ticket-reading preview requires " + kit.TICKET_READING_SPEC)
    tools = prerequisites()
    output = output.absolute()
    if output.exists() or any(
        parent.is_symlink() for parent in (output, *output.parents)
    ):
        raise RuntimeError("Evidence destination must be a new path without symlinks")
    output.mkdir(parents=True)
    runtime = Path(tempfile.mkdtemp(prefix="pb-synthetic-"))
    (runtime / "SYNTHETIC_ONLY").write_text(MARKER)
    api_port, web_port, tls_port = ports()
    before = kit.application_fingerprint()
    harness_files = (
        "build-kit/scripts/synthetic_rehearsal.py",
        "build-kit/scripts/synthetic_api.py",
        "build-kit/scripts/kit.py",
        "web/tests/synthetic-fixtures.ts",
    )
    if project is not None:
        harness_files += (
            "build-kit/scripts/synthetic_browser_matrix.py",
            "web/playwright.beta.config.ts",
            "web/package.json",
            "web/package-lock.json",
        )
    initial_harness = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in harness_files
    }
    worker, lob = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    env = clean_environment()
    env.update(
        {
            "SYNTHETIC_REHEARSAL": "1",
            "PB_PROVIDER_MODE": "fake",
            "PB_SERVICE_PHASE": service_phase,
            "PB_AUTH_MODE": "",
            "PB_DATA_DIR": str(runtime / "data"),
            "PB_WORKER_SECRET": worker,
            "PB_DEV_WORKER": "0",
            "PB_SYNTHETIC_RUNTIME": str(runtime),
            "PB_SYNTHETIC_ORIGIN": ORIGIN,
            "PB_SYNTHETIC_WORKER_SECRET": worker,
            "PB_SYNTHETIC_LOB_SECRET": lob,
            "PB_API_ORIGIN": f"http://127.0.0.1:{api_port}",
            "PYTHONPATH": os.pathsep.join(
                (str(ROOT / "backend/src"), str(ROOT / "build-kit/scripts"))
            ),
            "NEXT_PUBLIC_LIVE_API": "1",
            "NEXT_PUBLIC_PREVIEW_AUTH": "0",
            "NEXT_PUBLIC_OFFLINE_FIXTURES": "1",
            "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": "pk_test_synthetic_rehearsal",
            "NEXT_TELEMETRY_DISABLED": "1",
        }
    )
    processes = OwnedProcesses()
    result = {
        "synthetic_only": True,
        "production_web": True,
        "service_phase": service_phase,
        "ticket_reading_preview": ticket_reading_preview,
        "runtime": str(runtime),
        "application_fingerprint": before,
        "passed": False,
        "qualification": "Local synthetic rehearsal only; not task or launch acceptance",
    }
    if project is not None:
        result.update(
            {
                "browser_project": project,
                "browser_config": "web/playwright.beta.config.ts",
                "physical_camera": False,
                "mobile_viewport_emulation_only": True,
            }
        )
    try:
        private_web = prepare_web(runtime, output)
        result["private_web_build"] = True
        result["web_source_manifest_sha256"] = hashlib.sha256(
            (output / "web-source.json").read_bytes()
        ).hexdigest()
        print("Building production web with fake-provider configuration...", flush=True)
        processes.run(
            [tools["node"], tools["next"], "build", "--webpack"],
            env,
            output / "web-build.log",
            240,
            cwd=private_web,
        )
        processes.run(
            [
                tools["openssl"],
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(runtime / "key.pem"),
                "-out",
                str(runtime / "cert.pem"),
                "-days",
                "1",
                "-subj",
                "/CN=portal.example.test",
            ],
            env,
            output / "tls.log",
            30,
        )
        api = processes.launch(
            [
                tools["python"],
                "-m",
                "uvicorn",
                "synthetic_api:create_rehearsal_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--no-access-log",
            ],
            env,
            output / "api.log",
        )
        wait_fake(f"http://127.0.0.1:{api_port}/health", [api])
        web_env = {**env, "NODE_ENV": "production"}
        web = processes.launch(
            [
                tools["node"],
                tools["next"],
                "start",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(web_port),
            ],
            web_env,
            output / "web.log",
            cwd=private_web,
        )
        wait_fake(f"http://127.0.0.1:{web_port}/api/health", [api, web])
        tls = processes.launch(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--proxy",
                str(runtime),
                "--web-port",
                str(web_port),
                "--tls-port",
                str(tls_port),
            ],
            env,
            output / "proxy.log",
        )
        wait_fake(f"https://127.0.0.1:{tls_port}/api/health", [api, web, tls])
        env.update(
            {
                "SECURE_BASE": ORIGIN,
                "SYNTHETIC_BASE_URL": ORIGIN,
                "SECURE_TRANSPORT_BASE": f"https://127.0.0.1:{tls_port}",
                "PRODUCTION_BASE": f"http://127.0.0.1:{web_port}",
                "INGRESS_API_BASE": f"http://127.0.0.1:{api_port}",
                "INGRESS_WORKER_SECRET": worker,
                "T37_LOB_WEBHOOK_SECRET": lob,
                "T37_DATA_DIR": str(runtime / "data"),
                "T37_EVIDENCE_DIR": str(output / "browser"),
                "SYNTHETIC_CLOCK_PATH": str(runtime / "clock-command"),
                "DEST_BASE": ORIGIN,
                "PLAYWRIGHT_CHROME_PATH": tools["chrome"],
                "PLAYWRIGHT_HOST_RULES": f"MAP portal.example.test 127.0.0.1:{tls_port}",
            }
        )
        if project is not None:
            # The beta config declares all four projects, so the bounded CONNECT
            # transport is owned for every selected project, not only Firefox.
            proxy_port = ports(1)[0]
            connect = processes.launch(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--connect-proxy",
                    "--proxy-port",
                    str(proxy_port),
                    "--tls-port",
                    str(tls_port),
                ],
                env,
                output / "connect-proxy.log",
            )
            wait_tcp(proxy_port, [api, web, tls, connect])
            result["connect_proxy"] = {
                "mode": "http-connect-loopback",
                "allowed_destination": "portal.example.test:443",
                "tunnel_target": f"127.0.0.1:{tls_port}",
                "origin": f"http://127.0.0.1:{proxy_port}",
                "pid": connect.pid,
                "log": "connect-proxy.log",
            }
            env["PLAYWRIGHT_FIREFOX_PROXY"] = f"http://127.0.0.1:{proxy_port}"
        print(
            "Running required browser suites; skips and missing results fail the gate...",
            flush=True,
        )
        check_command = [
            sys.executable,
            "build-kit/scripts/kit.py",
            "run-check",
            kit.synthetic_browser_check(service_phase),
        ]
        if project is not None:
            check_command += ["--project", project]
        check = processes.launch(
            check_command,
            env,
            output / "browser-command.log",
        )
        code = check.wait(timeout=1800)
        observed = json.loads(
            (output / "browser-command.log").read_text().splitlines()[-1]
        )
        receipt_path = ROOT / "build-kit" / observed["receipt"]
        receipt = json.loads(receipt_path.read_text())
        result["browser_check_receipt"] = str(receipt_path.relative_to(ROOT))
        result["browser_check_sha256"] = hashlib.sha256(
            receipt_path.read_bytes()
        ).hexdigest()
        result["browser_report_validation"] = receipt.get("browser_report_validation")
        result["code_unchanged_during_rehearsal"] = (
            before == kit.application_fingerprint()
        )
        result["passed"] = (
            code == 0
            and observed_browser_receipt_valid(kit, receipt, before, service_phase, project, ticket_reading_preview)
            and result["code_unchanged_during_rehearsal"]
        )
    except KeyboardInterrupt:
        result["error"] = "Synthetic rehearsal interrupted"
    except (
        RuntimeError,
        OSError,
        ValueError,
        KeyError,
        subprocess.TimeoutExpired,
    ) as error:
        result["error"] = str(error)
    finally:
        result["owned_services_stopped"] = processes.close()
        result["owned_process_groups"] = [child.pid for child in processes.children]
        if project is not None:
            proxy_log = output / "connect-proxy.log"
            result["connect_proxy"] = {
                **result.get("connect_proxy", {}),
                "stopped": processes.stopped.get(result.get("connect_proxy", {}).get("pid"), False),
                "log_sha256": (
                    hashlib.sha256(proxy_log.read_bytes()).hexdigest()
                    if proxy_log.is_file()
                    else None
                ),
            }
        result["harness_sha256"] = {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in harness_files
        }
        result["initial_harness_sha256"] = initial_harness
        result["harness_unchanged_during_rehearsal"] = (
            initial_harness == result["harness_sha256"]
        )
        result["passed"] = (
            result["passed"]
            and result["owned_services_stopped"]
            and result["harness_unchanged_during_rehearsal"]
        )
        (output / "rehearsal.json").write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps(
                {"passed": result["passed"], "evidence": str(output / "rehearsal.json")}
            ),
            flush=True,
        )
    return 0 if result["passed"] else 1


def build_parser(kit) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        help="New evidence directory (default: build-kit/evidence/synthetic-<id>)",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check installed tools; start no services",
    )
    parser.add_argument(
        "--browser-project",
        choices=tuple(kit.BETA_BROWSER_PROJECTS),
        help="Run one explicit controlled-free beta project (beta config only)",
    )
    parser.add_argument(
        "--ticket-reading-preview",
        action="store_true",
        help="Set PB_TICKET_READING_PREVIEW=1 and require the ticket-vision browser suite",
    )
    parser.add_argument("--proxy", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--connect-proxy",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--service-phase",
        choices=("paid", "controlled_free_beta"),
        default="paid",
        help="Server phase to exercise with fake providers (default: paid)",
    )
    parser.add_argument("--web-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--proxy-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--tls-port", type=int, help=argparse.SUPPRESS)
    return parser


def main(argv=None) -> int:
    kit = kit_module()
    args = build_parser(kit).parse_args(argv)
    if args.ticket_reading_preview:
        os.environ["PB_TICKET_READING_PREVIEW"] = "1"
    if args.proxy is not None:
        proxy(args.proxy, args.web_port, args.tls_port)
        return 0
    if args.connect_proxy:
        connect_proxy(args.proxy_port, args.tls_port)
        return 0
    if args.preflight:
        print(
            json.dumps(
                {"synthetic_dependencies": prerequisites(), "providers": "fake only"},
                indent=2,
            )
        )
        return 0
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    return rehearse(
        args.out or ROOT / f"build-kit/evidence/synthetic-{time.time_ns()}",
        args.service_phase,
        args.browser_project,
    )


if __name__ == "__main__":
    raise SystemExit(main())
