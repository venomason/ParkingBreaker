"""Check the image-only Compose override shipped with an immutable private release."""

import json
import re
import sys
from pathlib import Path


def backend_image(path: Path, expected_commit: str | None) -> str:
    release = json.loads(path.read_bytes())
    if not isinstance(release, dict) or set(release) != {
        "x-pb-build-commit",
        "services",
    }:
        raise ValueError
    commit = release["x-pb-build-commit"]
    if not isinstance(commit, str) or not re.fullmatch("[0-9a-f]{40}", commit):
        raise ValueError
    if expected_commit is not None and expected_commit != commit:
        raise ValueError
    services = release["services"]
    if not isinstance(services, dict) or set(services) != {
        "api",
        "worker",
        "monitor",
        "caddy",
        "scheduler",
    }:
        raise ValueError
    for service in services.values():
        if not isinstance(service, dict) or set(service) != {"image"}:
            raise ValueError
        ref = service["image"]
        if not isinstance(ref, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}", ref
        ):
            raise ValueError
    image = services["api"]["image"]
    if services["worker"]["image"] != image or services["monitor"]["image"] != image:
        raise ValueError
    return image


if __name__ == "__main__":
    try:
        print(
            backend_image(
                Path(sys.argv[1]), sys.argv[2] if len(sys.argv) == 3 else None
            )
        )
    except (OSError, ValueError, RecursionError):
        sys.exit(
            "private release requires a valid image-only lock for the reviewed commit"
        )
