"""Pure target-host deployment validation; callers supply metadata and filesystem facts."""

from collections.abc import Mapping


def protected_env(uid: int, mode: int, regular: bool) -> bool:
    return uid == 0 and mode == 0o600 and regular


def regional_owner(disk: Mapping[str, object], vm: str, device: str) -> bool:
    zones = disk.get("replicaZones")
    name = disk.get("name")
    region = disk.get("region")
    return (
        isinstance(name, str)
        and bool(name)
        and device == f"/dev/disk/by-id/google-{name}"
        and isinstance(region, str)
        and region.endswith("/regions/us-west1")
        and isinstance(zones, list)
        and len(zones) == 2
        and all(isinstance(z, str) and "/zones/us-west1-" in z for z in zones)
        and zones[0] != zones[1]
        and disk.get("users") == [vm]
        and disk.get("accessMode", "READ_WRITE_SINGLE") == "READ_WRITE_SINGLE"
        and disk.get("status") == "READY"
    )
