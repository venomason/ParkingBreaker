"""Pure target-host deployment validation; callers supply metadata and filesystem facts."""

from collections.abc import Mapping
from ipaddress import IPv4Address, IPv4Network


def protected_env(uid: int, mode: int, regular: bool) -> bool:
    return uid == 0 and mode == 0o600 and regular


def private_gateway(frontend_subnet: str, vm_ip: str) -> bool:
    try:
        subnet = IPv4Network(frontend_subnet)
        address = IPv4Address(vm_ip)
    except ValueError:
        return False
    private = tuple(
        IPv4Network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    )
    return any(subnet.subnet_of(block) for block in private) and any(
        address in block for block in private
    )


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


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3 or not private_gateway(sys.argv[1], sys.argv[2]):
        sys.exit("private gateway needs an RFC1918 frontend subnet and a private VM IPv4 address")
