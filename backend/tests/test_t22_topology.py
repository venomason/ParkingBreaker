"""Production topology contracts; config rendering does not start Docker."""

import json
import shutil
import subprocess
from pathlib import Path

from parkingbreaker.transaction_store import SqliteTransactionStore

ROOT = Path(__file__).resolve().parents[2]


def render(tmp_path: Path, env_lines: str) -> subprocess.CompletedProcess[str]:
    env = tmp_path / "app.env"
    env.write_text(env_lines)
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env),
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(ROOT / "deploy/compose.production.yaml"),
            "config",
            "--format",
            "json",
        ],
        env={
            "PATH": str(Path(shutil.which("docker") or "/usr/bin/docker").parent),
            "PB_ENV_FILE": str(env),
            "PB_DATA_PATH": str(tmp_path / "data"),
            "PB_TLS_PATH": str(tmp_path / "tls"),
            "PB_DOMAIN": "offline.example.invalid",
        },
        capture_output=True,
        text=True,
        check=False,
    )


SYNTHETIC_ENV = (
    "PB_WORKER_SECRET=synthetic-only-worker-secret-0123456789\n"
    "PB_PROVIDER_MODE=fake\n"
    "PB_SERVICE_PHASE=controlled_free_beta\n"
)


def test_production_compose_has_only_tls_ingress(tmp_path: Path) -> None:
    result = render(tmp_path, SYNTHETIC_ENV)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert set(services) == {"api", "worker", "web", "scheduler", "caddy", "monitor"}
    for name in ("api", "worker", "web", "scheduler"):
        assert not services[name].get("ports")
        assert str(services[name]["user"]).split(":")[0] not in {"root", "0"}
        assert services[name]["security_opt"] == ["no-new-privileges:true"]
    assert {p["published"] for p in services["caddy"]["ports"]} == {"80", "443"}
    # Ingress listens on the real HTTP/HTTPS ports so the automatic redirect needs no port
    # remapping; only the bind capability is granted, never root or the Docker socket.
    assert {(p["published"], p["target"]) for p in services["caddy"]["ports"]} == {
        ("80", 80),
        ("443", 443),
    }
    assert services["caddy"]["cap_drop"] == ["ALL"]
    assert services["caddy"]["cap_add"] == ["NET_BIND_SERVICE"]
    assert str(services["caddy"]["user"]).split(":")[0] not in {"root", "0"}
    assert services["caddy"]["sysctls"] == {"net.ipv4.ip_unprivileged_port_start": "0"}
    assert not any("docker.sock" in str(v) for v in services["caddy"]["volumes"])
    # The web image is built for exactly the provider mode the env file selected.
    assert services["web"]["build"]["args"]["PB_PROVIDER_MODE"] == "fake"
    api_mount = next(v for v in services["api"]["volumes"] if v["target"] == "/data")
    assert api_mount["type"] == "bind"
    assert api_mount["source"] == str(tmp_path / "data")
    # T31: research, mailing and capture sweeps run only in the unpublished worker, on the
    # same database, so they cannot hold up customer requests on the api process.
    worker_mount = next(v for v in services["worker"]["volumes"] if v["target"] == "/data")
    assert worker_mount == api_mount
    assert services["api"]["environment"]["PB_API_ROLE"] == "customer"
    assert services["worker"]["environment"]["PB_API_ROLE"] == "worker"
    assert services["worker"]["cap_drop"] == ["ALL"]
    assert "http://worker:8000/internal/dispatch" in " ".join(services["scheduler"]["command"])
    assert "api:8000" not in " ".join(services["scheduler"]["command"])
    assert "PB_WORKER_SECRET" not in services["web"].get("environment", {})
    caddy = (ROOT / "deploy/Caddyfile").read_text()
    assert "reverse_proxy web:3000" in caddy
    assert 'Strict-Transport-Security "max-age=15552000"' in caddy
    assert "tls internal" not in caddy
    assert "http_port" not in caddy
    assert "https_port" not in caddy


def test_production_refuses_an_env_file_without_an_explicit_provider_mode(tmp_path: Path) -> None:
    """compose.yaml defaults to fake providers; a public production host must never inherit that."""
    # The phase is supplied so the only missing required variable is the provider mode.
    result = render(
        tmp_path,
        "PB_WORKER_SECRET=synthetic-only-worker-secret-0123456789\n"
        "PB_SERVICE_PHASE=controlled_free_beta\n",
    )
    assert result.returncode != 0
    assert "PB_PROVIDER_MODE" in result.stderr


def test_production_refuses_an_env_file_without_an_explicit_service_phase(tmp_path: Path) -> None:
    """Provider mode says what talks to providers; the phase says what the server serves."""
    result = render(
        tmp_path,
        "PB_WORKER_SECRET=synthetic-only-worker-secret-0123456789\nPB_PROVIDER_MODE=fake\n",
    )
    assert result.returncode != 0
    assert "PB_SERVICE_PHASE" in result.stderr


def test_production_api_and_worker_render_the_same_explicit_service_phase(
    tmp_path: Path,
) -> None:
    for phase in ("paid", "controlled_free_beta"):
        env_lines = SYNTHETIC_ENV.replace(
            "PB_SERVICE_PHASE=controlled_free_beta", f"PB_SERVICE_PHASE={phase}"
        )
        result = render(tmp_path, env_lines)
        assert result.returncode == 0, result.stderr
        services = json.loads(result.stdout)["services"]
        assert services["api"]["environment"]["PB_SERVICE_PHASE"] == phase
        assert services["worker"]["environment"]["PB_SERVICE_PHASE"] == phase
        # Web learns the phase from the API; no build arg or public flag may fork it.
        assert "PB_SERVICE_PHASE" not in services["web"].get("environment", {})
        assert "PB_SERVICE_PHASE" not in services["web"].get("build", {}).get("args", {})


def test_store_connection_is_full_wal(tmp_path: Path) -> None:
    with SqliteTransactionStore(tmp_path / "db.sqlite") as store:
        assert store._require_open().execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert store._require_open().execute("PRAGMA synchronous").fetchone() == (2,)


def dockerfile_stages() -> dict[str, list[str]]:
    stages: dict[str, list[str]] = {}
    current = ""
    for line in (ROOT / "backend/Dockerfile").read_text().splitlines():
        words = line.split()
        if len(words) >= 4 and words[0] == "FROM" and words[2] == "AS":
            current = words[3]
            stages[current] = []
        elif current and line.strip() and not line.lstrip().startswith("#"):
            stages[current].append(line)
    return stages


def test_optional_renderer_and_ocr_tools_stay_out_of_the_default_runtime_image() -> None:
    """Prepared, never enabled: tools exist only in a named non-default stage."""
    text = (ROOT / "backend/Dockerfile").read_text()
    stages = dockerfile_stages()
    assert list(stages)[-1] == "runtime"  # compose.yaml builds the last stage, no `target:`
    assert "target" not in (ROOT / "compose.yaml").read_text()
    tools = "\n".join(stages["document-tools"])
    for package in ("poppler-utils", "tesseract-ocr", "chromium"):
        assert package in tools
        assert package not in "\n".join(stages["base"] + stages["runtime"])
    assert "apt-get" not in "\n".join(stages["runtime"])
    # Every stage ends as the unprivileged app user, and nothing enables a feature.
    for name, lines in stages.items():
        users = [line for line in lines if line.startswith("USER ")]
        if users:
            assert users[-1] == "USER app", name
    assert "ENTRYPOINT" not in text
    assert text.count("CMD ") == 1
