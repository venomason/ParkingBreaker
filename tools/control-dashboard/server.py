#!/usr/bin/env python3
"""ParkingBreaker Control Dashboard — private loopback-only operator console.

Aggregates read-only live telemetry from ParkingBreaker services:
- pb-api (/health, /auth/pricing)
- pb-worker (/health, /internal/overseer/feed via T75 Bearer secret)
- pb-web (/, /terms, /refund-policy, /privacy, /robots.txt, /sitemap.xml)
- Local verification receipts (docs/verification/*.json)
- Launch gate board (~/Workspace/pb-coordination/GATE-STATUS.md)

Strict safety boundaries:
- Binds to 127.0.0.1 only (loopback).
- Read-only telemetry only; never exposes customer PII or write-side payment controls.
- Never leaks PB_OVERSEER_FEED_SECRET into responses or logs.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import socketserver
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_COORDINATION_DIR = Path.home() / "Workspace" / "pb-coordination"
DEFAULT_SECRET_FILE = Path.home() / ".pb-secrets" / "overseer-qual.secret"


def _read_secret(secret_file: Path | None) -> str:
    env_val = os.environ.get("PB_OVERSEER_FEED_SECRET", "").strip()
    if env_val:
        return env_val
    if secret_file and secret_file.is_file():
        return secret_file.read_text(encoding="utf-8").strip()
    return ""


def _http_probe(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 4.0,
    parse_json: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            body = resp.read()
            result: dict[str, Any] = {
                "ok": 200 <= resp.status < 300,
                "status": resp.status,
                "latency_ms": elapsed_ms,
                "bytes": len(body),
            }
            if parse_json:
                try:
                    result["data"] = json.loads(body.decode("utf-8"))
                except Exception as exc:
                    result["ok"] = False
                    result["error"] = f"invalid_json: {exc}"
            return result
    except urllib.error.HTTPError as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return {
            "ok": False,
            "status": exc.code,
            "latency_ms": elapsed_ms,
            "error": f"HTTP {exc.code}",
        }
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return {
            "ok": False,
            "status": None,
            "latency_ms": elapsed_ms,
            "error": type(exc).__name__,
        }


def _parse_gate_markdown(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    gates: list[dict[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("|") or line.startswith("|---") or "| Gate |" in line:
            continue
        parts = [cell.strip() for cell in line.strip("|").split("|")]
        if len(parts) < 3:
            continue
        gate_name, status_raw, remaining = parts[0], parts[1], parts[2]
        status_clean = re.sub(r"\*\*", "", status_raw)
        badge = "INFO"
        for tag in ("DONE", "PARTIAL", "BLOCKED", "OWNER"):
            if tag in status_clean.upper():
                badge = tag
                break
        gates.append(
            {
                "gate": gate_name,
                "badge": badge,
                "status": status_clean,
                "remaining": remaining,
            }
        )
    return gates


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _collect_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    api_base: str = cfg["api_base"].rstrip("/")
    worker_base: str = cfg["worker_base"].rstrip("/")
    web_base: str = cfg["web_base"].rstrip("/")
    secret = _read_secret(cfg["secret_file"])

    api_health = _http_probe(f"{api_base}/health", parse_json=True)
    worker_health = _http_probe(f"{worker_base}/health", parse_json=True)
    pricing = _http_probe(f"{api_base}/auth/pricing", parse_json=True)

    overseer: dict[str, Any]
    if secret:
        overseer = _http_probe(
            f"{worker_base}/internal/overseer/feed",
            headers={"Authorization": f"Bearer {secret}"},
            parse_json=True,
        )
    else:
        overseer = {
            "ok": False,
            "status": None,
            "latency_ms": 0.0,
            "error": "overseer_secret_not_configured",
        }

    web_routes = ["/", "/terms", "/refund-policy", "/privacy", "/robots.txt", "/sitemap.xml"]
    web_probes = {route: _http_probe(f"{web_base}{route}", timeout=4.0) for route in web_routes}

    rehearsal = _load_json(REPO_ROOT / "docs" / "verification" / "rehearsal-2026-11-23.json")
    r06 = _load_json(REPO_ROOT / "docs" / "verification" / "r06-reconciliation-2026-11-21.json")
    gates = _parse_gate_markdown(cfg["coordination_dir"] / "GATE-STATUS.md")

    return {
        "observed_at": datetime.now(UTC).isoformat(),
        "target": cfg["target_label"],
        "endpoints": {
            "api": api_base,
            "worker": worker_base,
            "web": web_base,
        },
        "overseer_configured": bool(secret),
        "services": {
            "api_health": api_health,
            "worker_health": worker_health,
            "pricing": pricing,
            "web_routes": web_probes,
        },
        "overseer": overseer,
        "gates": gates,
        "receipts": {
            "rehearsal_latest": (rehearsal or {}).get("legal_integration_verified_2026_11_23")
            or (rehearsal or {}).get("final_candidate_runs_2026_11_23"),
            "p03_deployed": ((r06 or {}).get("gates") or {}).get("P03"),
            "p06_deployed": ((r06 or {}).get("gates") or {}).get("P06"),
        },
    }


def make_handler(cfg: dict[str, Any]) -> type[http.server.BaseHTTPRequestHandler]:
    index_html = (HERE / "index.html").read_bytes()

    class DashboardHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            # Suppress noisy access logs; never log headers or tokens.
            return

        def _send(self, status: int, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", index_html)
                return
            if path == "/api/snapshot":
                snapshot = _collect_snapshot(cfg)
                body = json.dumps(snapshot, indent=2).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
                return
            if path == "/health":
                body = json.dumps({"status": "ok", "dashboard": "parkingbreaker-control"}).encode(
                    "utf-8"
                )
                self._send(200, "application/json; charset=utf-8", body)
                return
            self._send(404, "application/json; charset=utf-8", b'{"detail":"not_found"}')

    return DashboardHandler


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="ParkingBreaker Control Dashboard (loopback-only)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (loopback only)")
    parser.add_argument("--port", type=int, default=3090, help="Bind port (default: 3090)")
    parser.add_argument("--api-base", default=os.environ.get("PB_DASH_API", "http://127.0.0.1:18000"))
    parser.add_argument(
        "--worker-base", default=os.environ.get("PB_DASH_WORKER", "http://127.0.0.1:18001")
    )
    parser.add_argument("--web-base", default=os.environ.get("PB_DASH_WEB", "http://127.0.0.1:13000"))
    parser.add_argument(
        "--target-label",
        default=os.environ.get("PB_DASH_TARGET", "pb-qual-backend-01 (us-west1-a · rehearsal)"),
    )
    parser.add_argument("--secret-file", type=Path, default=DEFAULT_SECRET_FILE)
    parser.add_argument("--coordination-dir", type=Path, default=DEFAULT_COORDINATION_DIR)
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Refusing non-loopback bind; Control Dashboard is loopback-only.")

    cfg = {
        "api_base": args.api_base,
        "worker_base": args.worker_base,
        "web_base": args.web_base,
        "target_label": args.target_label,
        "secret_file": args.secret_file,
        "coordination_dir": args.coordination_dir,
    }
    server = _ThreadedHTTPServer((args.host, args.port), make_handler(cfg))
    print(f"ParkingBreaker Control Dashboard listening on http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
