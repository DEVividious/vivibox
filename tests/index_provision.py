"""A provisioning run's program for the live tests, obviously not a real one: it does what the
test asks in /config/provision-asked.json and reports what it saw and reached to
/config/provision-report.json, from inside the run. Standard library only; index_probe beside it.

    {"mode": "ok" | "fail" | "hang" | "wait", "tries": [...], "download": "<https url>"}

ok: report, download, exit 0. fail: report, exit 1. hang: never ends. wait: report, then listen on
LISTEN until /config/go is there, then exit 0 (the test reaches in meanwhile)."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import index_probe  # noqa: E402

LISTEN = 39419


def download(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=20) as answer:
            return f"{answer.status} {len(answer.read(65536))}"
    except Exception as e:  # noqa: BLE001 - reported, not judged
        return f"failed: {type(e).__name__}: {e}"


def main() -> int:
    with open("/config/provision-asked.json") as f:
        asked = json.load(f)
    mode = asked.get("mode", "ok")
    if mode == "hang":
        while True:
            time.sleep(60)
    seen = index_probe.mounts()
    with open("/proc/net/dev") as f:
        interfaces = [line.split(":")[0].strip() for line in f.readlines()[2:]]
    report = {
        "mounts": seen,
        "interfaces": interfaces,
        "tries": index_probe.tries(asked.get("tries", [])),
        "download": download(asked["download"]) if asked.get("download") else "",
        "status": index_probe.status(),
        "environment": sorted(os.environ),
        "resolv": index_probe.read("/etc/resolv.conf"),
    }
    with open("/config/provision-report.json", "w") as f:
        json.dump(report, f)
    if mode == "fail":
        return 1
    if mode == "wait":
        listener = socket.create_server(("0.0.0.0", LISTEN))
        listener.settimeout(0.2)
        with open("/config/listening", "w"):
            pass
        while not os.path.exists("/config/go"):
            try:
                conn, _ = listener.accept()
                conn.close()
                with open("/config/reached", "a") as f:
                    f.write("one\n")
            except TimeoutError:
                pass
    with open("/config/provisioned", "w") as f:
        f.write("downloaded\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
