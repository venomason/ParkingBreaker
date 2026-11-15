"""Offline manifest/Compose-merge invariants, not live networking qualification.

This bounded snapshot omits compose.yaml. A deliberately hostile synthetic base
exercises the real Compose merger with the unchanged production overlay. No daemon,
image pull, resource creation or external request is involved.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


def cloud_run() -> dict[str, Any]:
    # The manifest uses YAML's JSON subset with ordinary YAML comment lines.
    text = (DEPLOY / "cloudrun-frontend.service.yaml").read_text()
    return dict(
        json.loads("\n".join(line for line in text.splitlines() if not line.startswith("#")))
    )


def render(tmp_path: Path, *, missing: str | None = None) -> subprocess.CompletedProcess[str]:
    env = tmp_path / "synthetic.env"
    env.write_text(
        "PB_PROVIDER_MODE=fake\nPB_SERVICE_PHASE=controlled_free_beta\n"
        "PB_WORKER_SECRET=synthetic-worker-only-secret-012345\n"
    )
    base = tmp_path / "base.json"
    base.write_text(
        json.dumps(
            {
                "services": {
                    "api": {
                        "image": "example.invalid/offline-api:fixture",
                        "ports": ["8000:8000"],
                        "environment": {"PB_DATA_DIR": "/wrong"},
                        "volumes": ["wrong-data:/data"],
                    },
                    "web": {
                        "image": "example.invalid/offline-web:fixture",
                        "ports": ["3000:3000"],
                        "environment": {"PB_WORKER_SECRET": "must-not-survive"},
                    },
                    "scheduler": {
                        "image": "example.invalid/offline-scheduler:fixture",
                        "entrypoint": ["sh", "-c"],
                        "ports": ["9000:9000"],
                        "depends_on": ["api"],
                    },
                },
                "volumes": {"wrong-data": {}},
            }
        )
    )
    values = {
        "PATH": os.defpath,
        "PB_ENV_FILE": str(env),
        "PB_DATA_PATH": str(tmp_path / "regional-disk/data"),
        "PB_TLS_PATH": str(tmp_path / "old-tls"),
        "PB_DOMAIN": "customer.invalid",
        "PB_PUBLIC_ORIGIN": "https://customer.invalid",
        "PB_PRIVATE_VM_IP": "10.40.0.2",
        "PB_PRIVATE_API_HOST": "api.private.invalid",
        "PB_PRIVATE_TLS_PATH": str(tmp_path / "private-tls"),
    }
    if missing is not None:
        del values[missing]
    return subprocess.run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT),
            "--env-file",
            str(env),
            "-p",
            "offline-overlay-check",
            "-f",
            str(base),
            "-f",
            str(DEPLOY / "compose.production.yaml"),
            "-f",
            str(DEPLOY / "compose.private-vm.yaml"),
            "config",
            "--format",
            "json",
        ],
        env=values,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


@pytest.fixture
def rendered(tmp_path: Path) -> dict[str, Any]:
    result = render(tmp_path)
    assert result.returncode == 0, result.stderr
    return dict(json.loads(result.stdout))


def test_frontend_is_next_on_3000_with_digest_and_bounded_capacity() -> None:
    manifest = cloud_run()
    assert manifest["apiVersion"] == "serving.knative.dev/v1"
    assert manifest["kind"] == "Service"
    template = manifest["spec"]["template"]
    spec = template["spec"]
    ingress, web = spec["containers"]
    assert web["name"] == "web"
    assert not web.get("ports")  # only the gateway is platform ingress
    assert ingress["ports"] == [{"name": "http1", "containerPort": 8080}]
    assert ingress["image"].endswith("@sha256:REPLACE_WITH_64_HEX_REVIEWED_DIGEST")
    assert json.loads(
        template["metadata"]["annotations"]["run.googleapis.com/container-dependencies"]
    ) == {"ingress": ["web"]}
    assert web["command"] == ["node", "node_modules/next/dist/bin/next"]
    assert web["args"] == ["start", "--hostname", "0.0.0.0", "--port", "3000"]
    assert web["image"].endswith("@sha256:REPLACE_WITH_64_HEX_REVIEWED_DIGEST")
    assert web["startupProbe"]["tcpSocket"]["port"] == 3000
    assert spec["containerConcurrency"] == 20
    assert spec["timeoutSeconds"] == 60
    assert template["metadata"]["annotations"]["autoscaling.knative.dev/maxScale"] == "2"
    assert web["resources"]["limits"] == {"cpu": "1", "memory": "512Mi"}


def test_frontend_has_no_provider_worker_overseer_secrets_or_durable_backend_storage() -> None:
    spec = cloud_run()["spec"]["template"]["spec"]
    ingress, web = spec["containers"]
    assert web["env"] == [
        {"name": "NODE_ENV", "value": "production"},
        {"name": "NEXT_TELEMETRY_DISABLED", "value": "1"},
    ]
    for forbidden in ("volumes", "volumeMounts", "envFrom", "valueFrom", "secretKeyRef"):
        assert forbidden not in json.dumps(spec)
    assert "PB_API_ORIGIN" not in json.dumps(web["env"])  # must be baked into the image
    for container in (web, ingress):
        assert not re.search(
            r"LOB|STRIPE|WORKER|OVERSEER|SECRET|TOKEN|COOKIE",
            json.dumps(container.get("env", [])),
        )
    assert spec["serviceAccountName"].startswith("parkingbreaker-web@")


def test_frontend_uses_direct_vpc_and_restricted_ingress() -> None:
    manifest = cloud_run()
    assert manifest["metadata"]["annotations"]["run.googleapis.com/ingress"] == (
        "internal-and-cloud-load-balancing"
    )
    annotations = manifest["spec"]["template"]["metadata"]["annotations"]
    assert annotations["run.googleapis.com/vpc-access-egress"] == "private-ranges-only"
    assert "run.googleapis.com/vpc-access-connector" not in annotations
    (interface,) = json.loads(annotations["run.googleapis.com/network-interfaces"])
    assert set(interface) == {"network", "subnetwork", "tags"}
    assert interface["tags"] == ["parkingbreaker-frontend"]
    assert "US_WEST1" in interface["subnetwork"]


def test_cloud_run_public_ingress_refuses_internal_paths_before_next() -> None:
    ingress, _web = cloud_run()["spec"]["template"]["spec"]["containers"]
    assert ingress["name"] == "ingress"
    assert ingress["command"] == ["/bin/sh"]
    script = ingress["args"][1]
    assert script.endswith("exec caddy run --config /tmp/Caddyfile --adapter caddyfile")
    assert "<<'PB_GATEWAY'" in script
    assert ":8080 {" in script
    assert "route {" in script
    assert "admin off" in script
    deny = re.search(r"@private path (.+)", script)
    assert deny is not None
    assert set(deny[1].split()) == {
        "/internal",
        "/internal/*",
        "/api/internal",
        "/api/internal/*",
        "/dev-worker",
        "/dev-worker/*",
        "/api/dev-worker",
        "/api/dev-worker/*",
    }
    assert script.index("respond @private 404") < script.index("reverse_proxy 127.0.0.1:3000")
    assert script.count("reverse_proxy") == 1
    assert not re.search(r"request_body|header_up|header_down|rewrite|uri ", script)


def test_gateway_is_the_only_published_listener_and_is_private_tls(
    rendered: dict[str, Any],
) -> None:
    services = rendered["services"]
    assert set(services) == {"api", "worker", "scheduler", "monitor", "caddy"}
    for name in ("api", "worker", "scheduler", "monitor"):
        assert not services[name].get("ports"), name
    gateway = services["caddy"]
    (port,) = gateway["ports"]
    assert (port["host_ip"], port["published"], port["target"]) == ("10.40.0.2", "8443", 8443)
    assert gateway["read_only"] is True
    assert not gateway.get("environment")
    assert set(gateway["depends_on"]) == {"api"}
    assert set(gateway["networks"]) == {"customer"}
    assert gateway["security_opt"] == ["no-new-privileges:true"]
    assert gateway["cap_drop"] == ["ALL"]
    assert not gateway.get("cap_add")
    (tls,) = gateway["volumes"]
    assert tls["target"] == "/tls"
    assert tls["read_only"] is True
    assert tls["bind"].get("create_host_path", False) is False
    assert "docker.sock" not in json.dumps(services)
    config = rendered["configs"]["private_customer_gateway"]["content"]
    assert "https://api.private.invalid:8443" in config
    assert "tls /tls/fullchain.pem /tls/privkey.pem" in config
    assert "auto_https off" in config
    assert "admin off" in config
    assert "tls internal" not in config


def test_gateway_denies_internal_paths_before_an_exact_customer_allowlist(
    rendered: dict[str, Any],
) -> None:
    config = rendered["configs"]["private_customer_gateway"]["content"]
    deny = re.search(r"@private path (.+)", config)
    assert deny is not None
    assert set(deny[1].split()) == {
        "/internal",
        "/internal/*",
        "/api/internal",
        "/api/internal/*",
        "/dev-worker",
        "/dev-worker/*",
        "/api/dev-worker",
        "/api/dev-worker/*",
    }
    allowed = re.search(r"@customer path (.+)", config)
    assert allowed is not None
    assert set(allowed[1].split()) == {
        "/health",
        "/auth",
        "/auth/*",
        "/workspaces",
        "/workspaces/*",
        "/cases",
        "/cases/*",
        "/webhooks",
        "/webhooks/*",
    }
    assert "route {" in config
    assert config.index("respond @private 404") < config.index("reverse_proxy @customer api:8000")
    assert config.index("reverse_proxy @customer api:8000") < config.index("respond 404")
    assert config.count("reverse_proxy") == 1
    assert "worker:8000" not in config
    # No body or header rewriting: preserve signed webhooks and browser auth/CSRF data.
    assert not re.search(r"request_body|header_up|header_down|rewrite|uri ", config)


def test_single_vm_disk_and_worker_dispatch_boundaries(
    rendered: dict[str, Any], tmp_path: Path
) -> None:
    services = rendered["services"]
    mounts = []
    for name in ("api", "worker", "monitor"):
        service = services[name]
        mount = next(v for v in service["volumes"] if v["target"] == "/data")
        assert mount["type"] == "bind"
        assert mount["source"] == str(tmp_path / "regional-disk/data")
        assert mount["bind"].get("create_host_path", False) is False
        mounts.append(mount)
        assert service["deploy"]["replicas"] == 1
        assert service["container_name"] == "parkingbreaker-" + name
        assert service["user"] == "10001:10001"
    assert mounts[0] == mounts[1] == mounts[2]
    for name, role in (("api", "customer"), ("worker", "worker")):
        env = services[name]["environment"]
        assert env["PB_API_ROLE"] == role
        assert env["PB_DATA_DIR"] == "/data"
        assert env["PB_PUBLIC_ORIGIN"] == "https://customer.invalid"
        assert env["PB_SERVICE_PHASE"] == "controlled_free_beta"
    scheduler = services["scheduler"]
    assert scheduler["deploy"]["replicas"] == 1
    assert set(scheduler["networks"]) == {"worker_control"}
    assert set(scheduler["depends_on"]) == {"worker"}
    assert scheduler["command"] == [
        "exec sh /pb/scheduler-loop.sh http://worker:8000/internal/dispatch"
    ]
    assert set(services["worker"]["networks"]) == {"worker_control", "worker_egress"}
    assert set(services["api"]["networks"]) == {"customer", "api_egress"}
    assert not set(services["worker"]["networks"]) & set(services["api"]["networks"])
    assert rendered["networks"]["worker_control"]["internal"] is True
    assert rendered["networks"]["customer"]["internal"] is True
    assert "parkingbreaker.health_cli" in services["monitor"]["command"]


@pytest.mark.parametrize(
    "variable",
    [
        "PB_PRIVATE_VM_IP",
        "PB_PRIVATE_API_HOST",
        "PB_PRIVATE_TLS_PATH",
        "PB_PUBLIC_ORIGIN",
        "PB_DATA_PATH",
    ],
)
def test_missing_private_configuration_fails_rendering(tmp_path: Path, variable: str) -> None:
    result = render(tmp_path, missing=variable)
    assert result.returncode != 0
    assert variable in result.stderr


def test_build_time_origin_and_unqualified_release_gates_are_explicit() -> None:
    manifest = (DEPLOY / "cloudrun-frontend.service.yaml").read_text()
    overlay = (DEPLOY / "compose.private-vm.yaml").read_text()
    assert "BUILD time" in manifest
    assert "PB_API_ORIGIN=https://" in manifest
    assert "refuses internal/development routes before Next.js" in manifest
    assert "lifecycle.sh" in overlay
    assert "before activation" in overlay
    assert "stop-before-start" in overlay
    assert "quarantine" in overlay
    assert "replicas are not disk fencing" in overlay
    assert "PB_API_ORIGIN" in (ROOT / "web/next.config.ts").read_text()
