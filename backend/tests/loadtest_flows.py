"""T31 load test: concurrent synthetic customers through the real app in fake provider mode.

Each flow is one appeal from intake to capture, through the same HTTP routes a customer and
the worker use: workspace, photo, intake, pre-hold, tier, card hold, research, draft,
adoption, signature, packet preview, approval and dispatch. Providers are the offline fakes
(Stripe transport, Lob client, fixture research); an optional delay is injected into every
Stripe request and Lob letter call to mimic slow providers. Nothing leaves the machine.

Run: uv run python tests/loadtest_flows.py --levels 1 5 20 50 --out <file.json>

Measured: wall time, completed flows per second, per-stage latency p50/p95/p99, peak resident
memory, and invariants (exactly one Lob letter and one capture per case, final state
CAPTURED, another customer cannot read the case). SQLite lock wait is not measured
separately; it is inside the stage latencies.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import resource
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from httpx2 import ASGITransport, AsyncClient
from PIL import Image, ImageDraw

from parkingbreaker import config_fakes
from parkingbreaker.api.app import create_app
from parkingbreaker.api.auth import Customer
from parkingbreaker.config import build_services, load_settings

SECRET = "worker-secret-0123456789"
WORKER = {"Authorization": f"Bearer {SECRET}"}


class LoadVerifier:
    """One synthetic customer per flow: token "load-<n>" is customer "load<n>"."""

    def verify(self, token: str) -> Customer | None:
        prefix, _, number = token.partition("-")
        return Customer(f"load{number}") if prefix == "load" and number.isdigit() else None


def _customer(index: int) -> dict[str, str]:
    return {"Authorization": f"Bearer load-{index}"}


STAGES = (
    "intake",
    "card_hold",
    "research",
    "draft",
    "adopt_and_sign",
    "preview_and_approve",
    "dispatch_to_capture",
)


def _png(ink: bool) -> str:
    image = Image.new("RGB", (120, 40), "white")
    if ink:
        ImageDraw.Draw(image).line((5, 30, 110, 8), fill="black", width=3)
    out = io.BytesIO()
    image.save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


@dataclass
class Counts:
    letters: Counter[str] = field(default_factory=Counter)
    captures: Counter[str] = field(default_factory=Counter)


def _instrument(counts: Counts, delay_s: float) -> Callable[[], None]:
    """Count and slow every fake provider call; returns a function that undoes it."""
    lob_create = config_fakes.AcceptingLobClient.create_letter
    stripe_request = config_fakes.FakeStripeTransport.request

    def create_letter(self: Any, request: Any, key: str) -> Any:
        time.sleep(delay_s)
        counts.letters[request.submission_token] += 1
        return lob_create(self, request, key)

    def request(self: Any, method: str, url: str, headers: Any, post_data: object = None) -> Any:
        time.sleep(delay_s)
        if method.lower() == "post" and url.endswith("/capture"):
            counts.captures[url.rsplit("/", 2)[-2]] += 1
        return stripe_request(self, method, url, headers, post_data)

    config_fakes.AcceptingLobClient.create_letter = create_letter  # type: ignore[method-assign,assignment]
    config_fakes.FakeStripeTransport.request = request  # type: ignore[method-assign]

    def undo() -> None:
        config_fakes.AcceptingLobClient.create_letter = lob_create  # type: ignore[method-assign]
        config_fakes.FakeStripeTransport.request = stripe_request  # type: ignore[method-assign]

    return undo


def intake_body(index: int, ref: str) -> dict[str, Any]:
    """A synthetic SFMTA meter ticket issued three days ago."""
    issued = (date.today() - timedelta(days=3)).isoformat()
    return {
        "rental_company_paid": "NOT_RENTAL",
        "ticket": {
            "citation_number": f"SYN-{index:05d}",
            "license_plate": "SYN123",
            "license_plate_state": "CA",
            "issuer_hint": "San Francisco Municipal Transportation Agency",
            "issued_city": "San Francisco",
            "issued_state": "CA",
            "violation_code": "SYN-METER",
            "violation_description": "Meter expired",
            "issue_date": issued,
            "stage": "first-level review",
        },
        "customer_position": {"statement": "The meter was broken when I parked."},
        "contact": {
            "name": "Alex Example",
            "phone": "415-555-0100",
            "email": "alex@example.test",
            "mailing_address": {
                "line1": "123 Example Street",
                "city": "San Francisco",
                "state": "CA",
                "zip_code": "94103",
            },
        },
        "citation_photo_ref": ref,
        "photo_refs": [ref],
    }


async def _ok(response: Any, status: int = 200) -> Any:
    if response.status_code != status:
        raise AssertionError(f"{response.request.url.path}: {response.status_code} {response.text}")
    return response.json()


async def _flow(
    client: AsyncClient, index: int, timings: dict[str, list[float]]
) -> tuple[str, dict[str, str]]:
    who = _customer(index)
    clock = time.perf_counter
    start = clock()
    workspace = (
        await _ok(
            await client.post("/workspaces", headers=who, json={"notes": "Meter broken."}), 201
        )
    )["workspace_id"]
    photo = {"content_type": "image/png", "data_base64": _png(False)}
    ref = (
        await _ok(
            await client.post(f"/workspaces/{workspace}/photos", headers=who, json=photo), 201
        )
    )["photo_ref"]
    intake = intake_body(index, ref)
    case = (
        await _ok(await client.post(f"/workspaces/{workspace}/intake", headers=who, json=intake))
    )["case_id"]
    offer = await _ok(await client.get(f"/cases/{case}/pre-hold", headers=who))
    if offer["kind"] != "offer":
        raise AssertionError(f"no offer: {offer}")
    mark = clock()
    timings["intake"].append(mark - start)

    await _ok(await client.put(f"/cases/{case}/tier", headers=who, json={"tier": "CERTIFIED"}))
    hold = await _ok(
        await client.post(
            f"/cases/{case}/authorization",
            headers=who,
            json={"terms_accepted": True, "payment_method_id": "pm_card_visa"},
        )
    )
    if hold["status"] != "authorized":
        raise AssertionError(f"hold not authorized: {hold}")
    timings["card_hold"].append(clock() - mark)
    mark = clock()

    status = await _ok(
        await client.post(
            f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "research"}
        )
    )
    if status["state"] == "NEEDS_CUSTOMER":
        status = await _ok(
            await client.post(
                f"/cases/{case}/answers",
                headers=who,
                json={"question_id": "VEHICLE_RELATIONSHIP", "answer": "REGISTERED_OWNER"},
            )
        )
    if status["state"] != "DRAFTING":
        raise AssertionError(f"research did not reach drafting: {status['state']}")
    timings["research"].append(clock() - mark)
    mark = clock()

    await _ok(
        await client.post(f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "draft"})
    )
    draft = await _ok(await client.get(f"/cases/{case}/draft", headers=who))
    timings["draft"].append(clock() - mark)
    mark = clock()

    await _ok(
        await client.post(
            f"/cases/{case}/adoption",
            headers=who,
            json={"text": draft["text"], "text_sha256": draft["text_sha256"]},
        )
    )
    signature = {"content_type": "image/png", "data_base64": _png(True)}
    sig = (
        await _ok(
            await client.post(f"/workspaces/{workspace}/photos", headers=who, json=signature), 201
        )
    )["photo_ref"]
    timings["adopt_and_sign"].append(clock() - mark)
    mark = clock()

    body = {"adopted_sha256": draft["text_sha256"], "signature_ref": sig}
    preview = await _ok(await client.post(f"/cases/{case}/packet-preview", headers=who, json=body))
    frozen = await _ok(
        await client.post(
            f"/cases/{case}/approval",
            headers=who,
            json={
                **body,
                "packet_sha256": preview["packet_sha256"],
                "envelope_sha256": preview["envelope_sha256"],
            },
        )
    )
    if frozen["state"] != "FROZEN":
        raise AssertionError(f"approval did not freeze: {frozen['state']}")
    timings["preview_and_approve"].append(clock() - mark)
    mark = clock()

    final: dict[str, Any] = {}
    for _ in range(8):
        await _ok(
            await client.post(
                f"/internal/cases/{case}/step", headers=WORKER, json={"step_id": "dispatch"}
            )
        )
        final = await _ok(await client.get(f"/cases/{case}/status", headers=who))
        if final["payment_state"] == "CAPTURED":
            break
    if final.get("payment_state") != "CAPTURED":
        raise AssertionError(f"not captured: {final.get('state')} {final.get('payment_state')}")
    timings["dispatch_to_capture"].append(clock() - mark)
    return str(case), who


def _percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def at(p: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(p * len(ordered)))] * 1000, 1)

    return {"p50_ms": at(0.50), "p95_ms": at(0.95), "p99_ms": at(0.99), "n": len(ordered)}


async def run_level(concurrency: int, delay_ms: float = 0.0) -> dict[str, Any]:
    counts = Counts()
    undo = _instrument(counts, delay_ms / 1000)
    try:
        with tempfile.TemporaryDirectory() as data:
            settings = load_settings(
                {"PB_PROVIDER_MODE": "fake", "PB_WORKER_SECRET": SECRET, "PB_DATA_DIR": data},
                Path(data) / "no.env",
            )
            services = build_services(settings)
            app = create_app(services, LoadVerifier(), worker_secret=settings.worker_secret)
            timings: dict[str, list[float]] = defaultdict(list)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://load.test", timeout=600
            ) as client:
                started = time.perf_counter()
                results = await asyncio.gather(
                    *(_flow(client, index, timings) for index in range(concurrency)),
                    return_exceptions=True,
                )
                wall = time.perf_counter() - started
                failures = [repr(r) for r in results if isinstance(r, BaseException)]
                cases = [r for r in results if not isinstance(r, BaseException)]
                leaks = 0
                unexpected = 0
                # A valid customer who owns none of these cases must get "not found".
                stranger = _customer(1_000_000)
                for case, _who in cases:
                    response = await client.get(f"/cases/{case}/status", headers=stranger)
                    leaks += response.status_code < 400
                    unexpected += response.status_code != 404
    finally:
        undo()
    return {
        "concurrency": concurrency,
        "provider_delay_ms": delay_ms,
        "completed": len(cases),
        "failures": failures[:5],
        "failure_count": len(failures),
        "wall_s": round(wall, 2),
        "flows_per_s": round(len(cases) / wall, 2) if wall else None,
        "stages": {stage: _percentiles(timings[stage]) for stage in STAGES if timings[stage]},
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "letters_per_case_max": max(counts.letters.values(), default=0),
        "cases_with_letter": len(counts.letters),
        "captures_per_payment_max": max(counts.captures.values(), default=0),
        "payments_captured": len(counts.captures),
        "cross_customer_reads_allowed": leaks,
        "cross_customer_non_404": unexpected,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--levels", type=int, nargs="+", default=[1, 5, 20, 50])
    parser.add_argument("--delay-ms", type=float, nargs="+", default=[0.0])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = [
        asyncio.run(run_level(level, delay)) for delay in args.delay_ms for level in args.levels
    ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    for row in report:
        print(json.dumps({k: row[k] for k in row if k != "stages"}))


if __name__ == "__main__":
    main()
