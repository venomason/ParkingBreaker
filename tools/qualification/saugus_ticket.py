"""Generate two fictional photos for saugus-browser.cjs in a new directory."""

import argparse
import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", type=Path)
    out = parser.parse_args().out
    out.mkdir(mode=0o700, exist_ok=False)
    issued = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    ticket = "PB-SIM-" + secrets.token_hex(5).upper()
    sides = {
        "front": [
            "FICTIONAL PRIVATE TEST - NO PAYMENT DUE",
            "TOWN OF SAUGUS, MA",
            "PARKING TICKET",
            f"TICKET NUMBER: {ticket}",
            f"ISSUE DATE: {issued}",
            "LOCATION: Example Street",
            "VIOLATION: Expired meter",
            "STAGE: Initial appeal",
        ],
        "back": [
            "FICTIONAL PRIVATE TEST",
            "This test ticket does not print a notice date or an absolute deadline.",
            "The test driver will supply the plate during customer confirmation.",
        ],
    }
    font = ImageFont.truetype("DejaVuSans.ttf", 28)
    hashes = {}
    for side, lines in sides.items():
        image = Image.new("RGB", (1400, 600), "white")
        ImageDraw.Draw(image).multiline_text(
            (45, 45), "\n".join(lines), font=font, fill="black", spacing=12
        )
        file = out / f"{side}.png"
        image.save(file)
        hashes[side] = hashlib.sha256(file.read_bytes()).hexdigest()
    data = {
        "ticket": ticket,
        "fictional": True,
        "issue_date": issued,
        "photo_sha256": hashes,
    }
    (out / "manifest.json").write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    main()
