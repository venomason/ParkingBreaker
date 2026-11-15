"""T31 criterion 3, before and after the worker split: does slow research stall customers?

Starts real uvicorn processes in fake provider mode on one temporary database. The research
model fake is slowed (default 3 s per call) to stand in for a real model. Alice's case is
sent to research while Bob's customer requests are timed every 50 ms.

- before: one process with every route (PB_API_ROLE=all), as the stack ran until now.
- after: a customer process (PB_API_ROLE=customer) and a worker process (PB_API_ROLE=worker)
  on the same database; research goes to the worker.

Run: uv run python tests/loadtest_isolation.py --out <file.json>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx2 as httpx

SECRET = "worker-secret-0123456789"
WORKER = {"Authorization": f"Bearer {SECRET}"}
ALICE = {"Authorization": "Bearer fake-alice"}
BOB = {"Authorization": "Bearer fake-bob"}


def serve(role: str, port: int, delay_s: float) -> None:
    """Child process: slow the fake research model, then serve the app for this role."""
    import uvicorn

    from parkingbreaker import config_fakes

    complete = config_fakes.FixtureLlm.complete_json

    def slow(self: Any, *args: Any, **kwargs: Any) -> Any:
        time.sleep(delay_s)
        return complete(self, *args, **kwargs)

    config_fakes.FixtureLlm.complete_json = slow  # type: ignore[method-assign]
    from parkingbreaker.api.app import app_from_environment

    uvicorn.run(app_from_environment(), host="127.0.0.1", port=port, log_level="warning")


def _start(role: str, port: int, data: str, delay_s: float) -> subprocess.Popen[bytes]:
    env = {
        **os.environ,
        "PB_PROVIDER_MODE": "fake",
        "PB_WORKER_SECRET": SECRET,
        "PB_DATA_DIR": data,
        "PB_API_ROLE": role,
        # Research-only measurement: no hold is captured or released across processes here.
        "PB_FAKE_SPLIT_FOR_MEASUREMENT": "1",
    }
    process = subprocess.Popen(
        [sys.executable, __file__, "serve", role, str(port), str(delay_s)], env=env
    )
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return process
        except httpx.HTTPError:
            time.sleep(0.1)
    process.kill()
    raise RuntimeError(f"{role} server did not start")


def _held_case(base: str, who: dict[str, str]) -> str:
    sys.path.insert(0, str(Path(__file__).parent))
    from loadtest_flows import _png

    c = httpx.Client(base_url=base, timeout=60)
    workspace = c.post("/workspaces", headers=who, json={"notes": "Meter broken."}).json()
    ws = workspace["workspace_id"]
    ref = c.post(
        f"/workspaces/{ws}/photos",
        headers=who,
        json={"content_type": "image/png", "data_base64": _png(False)},
    ).json()["photo_ref"]
    from loadtest_flows import intake_body

    case = c.post(f"/workspaces/{ws}/intake", headers=who, json=intake_body(0, ref)).json()[
        "case_id"
    ]
    c.get(f"/cases/{case}/pre-hold", headers=who)
    c.put(f"/cases/{case}/tier", headers=who, json={"tier": "CERTIFIED"})
    hold = c.post(
        f"/cases/{case}/authorization",
        headers=who,
        json={"terms_accepted": True, "payment_method_id": "pm_card_visa"},
    ).json()
    if hold.get("status") != "authorized":
        raise RuntimeError(f"hold failed: {hold}")
    return str(case)


def _measure(customer_base: str, worker_base: str, delay_s: float) -> dict[str, Any]:
    alice = _held_case(customer_base, ALICE)
    bob = _held_case(customer_base, BOB)
    latencies: list[float] = []
    research: dict[str, float] = {}

    def run_research() -> None:
        started = time.perf_counter()
        httpx.post(
            f"{worker_base}/internal/cases/{alice}/step",
            headers=WORKER,
            json={"step_id": "research"},
            timeout=120,
        )
        research["seconds"] = time.perf_counter() - started

    thread = threading.Thread(target=run_research)
    thread.start()
    time.sleep(0.2)
    with httpx.Client(base_url=customer_base, timeout=120) as c:
        while thread.is_alive():
            started = time.perf_counter()
            c.get(f"/cases/{bob}/status", headers=BOB)
            latencies.append(time.perf_counter() - started)
            time.sleep(0.05)
    thread.join()
    ordered = sorted(latencies)
    return {
        "research_seconds": round(research.get("seconds", -1), 2),
        "customer_requests_during_research": len(ordered),
        "customer_p50_ms": round(ordered[len(ordered) // 2] * 1000, 1) if ordered else None,
        "customer_max_ms": round(ordered[-1] * 1000, 1) if ordered else None,
        "model_delay_s": delay_s,
    }


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve(sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay-s", type=float, default=3.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as data:
        single = _start("all", 8711, data, args.delay_s)
        try:
            report["before_single_process"] = _measure(
                "http://127.0.0.1:8711", "http://127.0.0.1:8711", args.delay_s
            )
        finally:
            single.terminate()
            single.wait()
    with tempfile.TemporaryDirectory() as data:
        customer = _start("customer", 8712, data, args.delay_s)
        worker = _start("worker", 8713, data, args.delay_s)
        try:
            report["after_worker_split"] = _measure(
                "http://127.0.0.1:8712", "http://127.0.0.1:8713", args.delay_s
            )
            report["customer_process_refuses_internal"] = (
                httpx.post("http://127.0.0.1:8712/internal/dispatch", headers=WORKER).status_code
                == 404
            )
        finally:
            for process in (customer, worker):
                process.terminate()
                process.wait()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
