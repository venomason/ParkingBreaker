"""Offline manifest/Compose-merge invariants, not live networking qualification.

Exercise the real Compose merger with both the release stack and an adverse base.
The fixtures use local image references with synthetic digests and need no daemon.
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


def write_image_lock(path: Path, commit: str = "1" * 40) -> Path:
    services = {
        name: {"image": f"registry.invalid/pb/{image}@sha256:{digest * 64}"}
        for name, image, digest in (
            ("api", "backend", "a"),
            ("worker", "backend", "a"),
            ("monitor", "backend", "a"),
            ("caddy", "gateway", "b"),
            ("scheduler", "scheduler", "c"),
        )
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"x-pb-build-commit": commit, "services": services}))
    return path


def cloud_run() -> dict[str, Any]:
    # The manifest uses YAML's JSON subset with ordinary YAML comment lines.
    text = (DEPLOY / "cloudrun-frontend.service.yaml").read_text()
    return dict(
        json.loads("\n".join(line for line in text.splitlines() if not line.startswith("#")))
    )


def render(
    tmp_path: Path,
    *,
    missing: str | None = None,
    real_base: bool = False,
    intake: str | None = None,
) -> subprocess.CompletedProcess[str]:
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
        "PB_BUILD_COMMIT": "1" * 40,
        "PB_PRIVATE_VM_IP": "10.40.0.2",
        "PB_PRIVATE_API_HOST": "api.private.invalid",
        "PB_PRIVATE_TLS_PATH": str(tmp_path / "private-tls"),
    }
    if missing is not None:
        del values[missing]
    if intake is not None:
        values["PB_NEW_INTAKE"] = intake
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
            str(ROOT / "compose.yaml" if real_base else base),
            "-f",
            str(DEPLOY / "compose.production.yaml"),
            "-f",
            str(DEPLOY / "compose.private-vm.yaml"),
            "-f",
            str(write_image_lock(tmp_path / "private-images.json")),
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


def private_gateway_file(stack: dict[str, Any]) -> str:
    # Compose mounts file-backed configs directly into a read-only container.
    config = stack["configs"]["private_customer_gateway"]
    assert "content" not in config
    path = Path(config["file"])
    assert path == DEPLOY / "Caddyfile.private"
    (mount,) = stack["services"]["caddy"]["configs"]
    assert mount["source"] == "private_customer_gateway"
    assert mount["target"] == "/etc/caddy/Caddyfile"
    return path.read_text()


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
    assert gateway["environment"] == {
        "PB_NEW_INTAKE": "open",
        "PB_PRIVATE_API_HOST": "api.private.invalid",
    }
    assert set(gateway["depends_on"]) == {"api"}
    assert set(gateway["networks"]) == {"customer", "gateway_ingress"}
    assert [
        name for name, service in services.items() if "gateway_ingress" in service["networks"]
    ] == ["caddy"]
    ingress = rendered["networks"]["gateway_ingress"]
    assert ingress["driver"] == "bridge"
    assert not ingress.get("internal", False)
    assert ingress["driver_opts"]["com.docker.network.bridge.enable_ip_masquerade"] == "false"
    assert gateway["security_opt"] == ["no-new-privileges:true"]
    assert gateway["cap_drop"] == ["ALL"]
    assert gateway["cap_add"] == ["NET_BIND_SERVICE"]
    assert gateway["user"] == "10001:10001"
    (tls,) = gateway["volumes"]
    assert tls["target"] == "/tls"
    assert tls["read_only"] is True
    assert tls["bind"].get("create_host_path", False) is False
    assert "docker.sock" not in json.dumps(services)
    config = private_gateway_file(rendered)
    assert "https://{$PB_PRIVATE_API_HOST}:8443" in config
    assert "tls /tls/fullchain.pem /tls/privkey.pem" in config
    assert "auto_https off" in config
    assert "admin off" in config
    assert "tls internal" not in config


def test_real_private_stack_only_runs_the_locked_images(tmp_path: Path) -> None:
    result = render(tmp_path, real_base=True)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    locked = json.loads((tmp_path / "private-images.json").read_text())["services"]
    assert {name: {"image": service["image"]} for name, service in services.items()} == locked
    assert all("build" not in service for service in services.values())


def test_new_intake_stop_is_scoped_to_new_workspace_and_submission(tmp_path: Path) -> None:
    result = render(tmp_path, real_base=True, intake="paused")
    assert result.returncode == 0, result.stderr
    stack = json.loads(result.stdout)
    assert stack["services"]["caddy"]["environment"]["PB_NEW_INTAKE"] == "paused"
    config = private_gateway_file(stack)
    assert "method POST" in config
    assert "path_regexp ^/(auth/appeals|workspaces(/[^/]+/intake)?)/?$" in config
    assert "not vars {env.PB_NEW_INTAKE} open" in config
    assert config.index("respond @intakePaused") < config.index("reverse_proxy @customer")
    assert "503" in config


def test_gateway_denies_internal_paths_before_an_exact_customer_allowlist(
    rendered: dict[str, Any],
) -> None:
    config = private_gateway_file(rendered)
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
        "/outcomes",
        "/outcomes/*",
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
        assert env["PB_BUILD_COMMIT"] == "1" * 40
        assert env["PB_DATA_DIR"] == "/data"
        assert env["PB_PUBLIC_ORIGIN"] == "https://customer.invalid"
        assert env["PB_SERVICE_PHASE"] == "controlled_free_beta"
    scheduler = services["scheduler"]
    assert scheduler["deploy"]["replicas"] == 1
    assert set(scheduler["networks"]) == {"worker_control"}
    assert set(scheduler["depends_on"]) == {"worker"}
    assert scheduler["command"] == [
        "exec sh /pb/scheduler-loop.sh http://worker:8000/internal/dispatch http://worker:8000/internal/follow-ups"
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
        "PB_BUILD_COMMIT",
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
