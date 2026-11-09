#!/usr/bin/env python3
"""Synchronize the YouTube advertiser registry from its owner-managed Sheet."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse


def _local_config() -> dict:
    """Owner Sheet location is private: read it from the environment or the untracked local_config.json."""
    path = Path(__file__).with_name("local_config.json")
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


_CONFIG = _local_config()
SPREADSHEET_ID = os.environ.get("TUBESPY_SHEET_ID") or _CONFIG.get("spreadsheet_id", "")
SHEET_GID = os.environ.get("TUBESPY_SHEET_GID") or _CONFIG.get("sheet_gid", "0")
SHEET_NAME = "YouTube Library Advertisers"
REQUIRED_HEADERS = {"Advertiser", "Google Ads Transparency URL", "Active"}


class RegistryError(ValueError):
    """Raised when the managed registry is unsafe or ambiguous."""


def _active(raw: str) -> bool:
    value = raw.strip().casefold()
    if value in {"", "true", "yes", "1", "active"}:
        return True
    if value in {"false", "no", "0", "inactive"}:
        return False
    raise RegistryError(f"invalid Active value: {raw!r}")


def _safe_transparency_url(raw: str) -> str:
    value = raw.strip()
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "adstransparency.google.com"
        or parsed.username
        or parsed.password
    ):
        raise RegistryError(f"unsafe Google Ads Transparency URL: {value}")
    query = parse_qs(parsed.query)
    platform = [item.casefold() for item in query.get("platform", [])]
    if "youtube" not in platform:
        raise RegistryError(f"URL is not filtered to YouTube: {value}")
    return value


def parse_registry_csv(csv_text: str, *, spreadsheet_id: str, sheet_gid: str) -> dict:
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    headers = set(reader.fieldnames or [])
    missing = REQUIRED_HEADERS - headers
    if missing:
        raise RegistryError(f"missing required columns: {', '.join(sorted(missing))}")

    advertisers = []
    seen = set()
    for row_number, row in enumerate(reader, start=2):
        name = (row.get("Advertiser") or "").strip()
        url = (row.get("Google Ads Transparency URL") or "").strip()
        active_raw = row.get("Active") or ""
        if not name and not url and not active_raw.strip():
            continue
        if not _active(active_raw):
            continue
        if not name:
            raise RegistryError(f"row {row_number}: missing advertiser")
        if not url:
            raise RegistryError(f"row {row_number}: missing Google Ads Transparency URL")
        identity = name.casefold()
        if identity in seen:
            raise RegistryError(f"row {row_number}: duplicate advertiser: {name}")
        seen.add(identity)
        advertisers.append(
            {
                "name": name,
                "source": "sheet",
                "transparency_url": _safe_transparency_url(url),
            }
        )

    if not advertisers:
        raise RegistryError("managed registry has no active advertisers")
    return {
        "schema_version": 2,
        "source": {
            "spreadsheet_id": spreadsheet_id,
            "sheet": SHEET_NAME,
            "sheet_gid": str(sheet_gid),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        },
        "advertisers": advertisers,
    }


def download_registry_csv(spreadsheet_id: str, sheet_gid: str) -> str:
    if not spreadsheet_id:
        raise RegistryError("set TUBESPY_SHEET_ID or create local_config.json with spreadsheet_id")
    query = urlencode({"format": "csv", "gid": sheet_gid})
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status != 200:
            raise RegistryError(f"registry export returned HTTP {response.status}")
        return response.read().decode("utf-8-sig")


def write_registry(path: Path, registry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(registry, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spreadsheet-id", default=SPREADSHEET_ID)
    parser.add_argument("--sheet-gid", default=SHEET_GID)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("registry.json"))
    args = parser.parse_args()
    csv_text = download_registry_csv(args.spreadsheet_id, args.sheet_gid)
    registry = parse_registry_csv(
        csv_text,
        spreadsheet_id=args.spreadsheet_id,
        sheet_gid=args.sheet_gid,
    )
    write_registry(args.output, registry)
    print(json.dumps({"advertisers": len(registry["advertisers"]), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
