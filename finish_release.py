#!/usr/bin/env python3
"""Guarded overnight completion: collect, validate, stage, publish, and read back."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "registry.json"
STAGE = ROOT / "stage" / "private-youtube-pilot"
SLUG = "cobalt-violet-hs8j"
ACCOUNT = "coolina"
ACCOUNT_URL = "https://youtube-competitor-library.coolina.here.now/"
PYTHON = sys.executable
LOG = ROOT / "overnight-release.log"
CURRENT_COLLECTOR_PID = int(os.environ.get("CURRENT_COLLECTOR_PID", "0"))


def emit(event: str, **values) -> None:
    row = {"event": event, **values}
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def run(command: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    emit("command_start", command=command)
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(result.stdout)
        if result.stdout and not result.stdout.endswith("\n"):
            handle.write("\n")
    emit("command_end", command=command, exit_code=result.returncode)
    return result


def incomplete() -> list[str]:
    registry = json.loads(REGISTRY.read_text())
    missing = []
    for item in registry["advertisers"]:
        name = item["name"]
        if name == "TQR Marketing Group":
            continue
        slug = __import__("re").sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        path = ROOT / "raw" / slug / "summary.json"
        if not path.exists():
            missing.append(name)
            continue
        summary = json.loads(path.read_text())
        if summary.get("status") != "complete":
            missing.append(name)
    return missing


def api_request(path: str) -> bytes:
    key = Path.home().joinpath(".herenow/credentials").read_text().strip()
    request = urllib.request.Request(
        "https://here.now" + path,
        headers={"Authorization": "Bearer " + key, "X-HereNow-Account": ACCOUNT},
    )
    return urllib.request.urlopen(request, timeout=60).read()


def verify_live() -> dict:
    manifest = json.loads(api_request(f"/api/v1/publish/{SLUG}/files"))
    remote_paths = sorted(item.get("path") for item in manifest.get("files", []) if item.get("path"))
    local_paths = sorted(str(path.relative_to(STAGE)) for path in STAGE.rglob("*") if path.is_file())
    if remote_paths != local_paths:
        raise RuntimeError(f"live manifest mismatch: remote={remote_paths}, local={local_paths}")
    digests = {}
    for relative in local_paths:
        local = (STAGE / relative).read_bytes()
        remote = api_request(f"/api/v1/publish/{SLUG}/files/{relative}")
        if remote != local:
            raise RuntimeError(f"live file mismatch: {relative}")
        digests[relative] = hashlib.sha256(local).hexdigest()
    access = json.loads(api_request(f"/api/v1/publish/{SLUG}/access"))
    mode = access.get("mode") or access.get("accessMode")
    if mode != "account_members":
        raise RuntimeError(f"unexpected access mode: {mode}")
    try:
        response = urllib.request.urlopen(ACCOUNT_URL + "?release=top-five", timeout=30)
        status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    if status != 200:
        raise RuntimeError(f"account URL status {status}")
    return {"files": local_paths, "sha256": digests, "access_mode": mode, "account_url_status": status}


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    if CURRENT_COLLECTOR_PID:
        emit("waiting_for_active_collector", pid=CURRENT_COLLECTOR_PID)
        while alive(CURRENT_COLLECTOR_PID):
            time.sleep(30)
    for attempt in range(1, 7):
        pending = incomplete()
        if not pending:
            break
        emit("collection_resume", attempt=attempt, pending=pending)
        result = run([PYTHON, "-u", "collect_all.py", "--concurrency", "1"])
        if result.returncode not in (0, 2):
            time.sleep(min(300, 30 * attempt))
    pending = incomplete()
    if pending:
        raise RuntimeError(f"collection incomplete after bounded retries: {pending}")

    tests = run(["/Users/maxopenclaw/.hermes/hermes-agent/.venv/bin/python", "-m", "pytest", "-q", "tests"], timeout=300)
    if tests.returncode:
        raise RuntimeError("test suite failed")
    final = run([PYTHON, "finalize_multi.py"], timeout=300)
    if final.returncode:
        raise RuntimeError("dataset finalization failed")

    server = subprocess.Popen([PYTHON, "-m", "http.server", "8767", "--bind", "127.0.0.1", "--directory", str(STAGE)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(30):
            try:
                if urllib.request.urlopen("http://127.0.0.1:8767/", timeout=2).status == 200:
                    break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("local QA server did not become ready")
        local_qa = run([PYTHON, "qa_local.py"], timeout=180)
        if local_qa.returncode:
            raise RuntimeError("local browser QA failed")
        playback = run([PYTHON, "qa_playback.py"], timeout=180)
        if playback.returncode:
            raise RuntimeError("local playback QA failed")
        playback_data = json.loads(playback.stdout)
        state = playback_data.get("playback") or {}
        if not (state.get("currentTime", 0) > 0 and state.get("error") is None):
            raise RuntimeError(f"playback did not advance: {state}")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    publish = run([
        "bash",
        "/Users/maxopenclaw/.hermes/skills/here-now/scripts/publish.sh",
        str(STAGE),
        "--slug", SLUG,
        "--workspace", ACCOUNT,
        "--client", "hermes",
    ], timeout=900)
    if publish.returncode:
        raise RuntimeError("here.now publication failed")
    if ACCOUNT_URL not in publish.stdout:
        raise RuntimeError("publish result did not confirm the target account URL")
    verification = verify_live()
    emit("release_complete", account_url=ACCOUNT_URL, verification=verification)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        emit("release_blocked", error=f"{type(error).__name__}: {error}")
        raise
