"""What a fully compromised index sees and reaches from inside its container, as JSON on stdout:
run there as `python3 - '<json of what to try>'`, as the index's own processes run (its user, no
capability). Standard library only.

It reports, without judging: the mounts, the canaries found in any file of any mount, the
interfaces, the processes, its own capabilities, which folders it can write, and what each
connection it was told to try does (tcp, udp, a unix socket by path, an abstract one)."""

from __future__ import annotations

import errno
import json
import os
import socket
import sys

SKIP_TYPES = {"proc", "sysfs", "cgroup", "cgroup2", "devpts", "mqueue", "securityfs", "debugfs"}


def mounts() -> list[dict]:
    found = []
    with open("/proc/self/mountinfo") as f:
        for line in f:
            left, _, right = line.partition(" - ")
            fields, after = left.split(), right.split()
            found.append(
                {
                    "root": fields[3],
                    "point": fields[4],
                    "options": fields[5],
                    "type": after[0],
                    "source": after[1],
                }
            )
    return found


def canaries(points: list[str], words: list[str]) -> list[str]:
    """Every file of these mounts that holds a canary, by its path; a file read up to 4 MiB."""
    hits = []
    for top in points:
        for folder, _dirs, files in os.walk(top, followlinks=False):
            for name in files:
                path = os.path.join(folder, name)
                try:
                    with open(path, "rb", buffering=0) as f:
                        data = f.read(4 * 2**20) if os.path.isfile(path) else b""
                except OSError:
                    continue
                hits += [f"{path}: {w}" for w in words if w.encode() in data]
    return hits


def tries(attempts: list[dict]) -> list[dict]:
    done = []
    for a in attempts:
        try:
            if a["kind"] == "tcp":
                s = socket.create_connection((a["host"], a["port"]), timeout=3)
            elif a["kind"] == "udp":
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(3)
                s.sendto(b"\0" * 12, (a["host"], a["port"]))
                s.recv(512)
            elif a["kind"] == "dns":
                socket.getaddrinfo(a["host"], 443)
                s = None
            else:  # unix by path, or abstract when the name starts with @
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(3)
                name = a["path"]
                s.connect("\0" + name[1:] if name.startswith("@") else name)
            done.append({**a, "result": "reached"})
            if s is not None:
                s.close()
        except socket.gaierror as e:
            code = {socket.EAI_AGAIN: "EAI_AGAIN", socket.EAI_NONAME: "EAI_NONAME"}.get(e.errno, str(e.errno))
            done.append({**a, "result": code})
        except OSError as e:
            code = errno.errorcode.get(e.errno, str(e.errno)) if e.errno else type(e).__name__
            done.append({**a, "result": code})
    return done


def writable(folders: list[str]) -> list[str]:
    can = []
    for folder in folders:
        path = os.path.join(folder, f".probe-{os.getpid()}")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            continue
        os.close(fd)
        os.unlink(path)
        can.append(folder)
    return can


def read(path: str) -> str:
    with open(path) as f:
        return f.read()


def status() -> dict:
    found = {}
    with open("/proc/self/status") as f:
        for line in f:
            key, _, value = line.partition(":")
            if key in ("CapEff", "CapPrm", "CapBnd", "NoNewPrivs", "Seccomp", "Uid"):
                found[key] = value.strip()
    return found


def processes() -> list[str]:
    found = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    found.append(f.read().replace(b"\0", b" ").decode(errors="replace").strip())
            except OSError:
                pass
    return found


def main() -> None:
    asked = json.loads(sys.argv[1])
    seen = mounts()
    walked = [m["point"] for m in seen if m["type"] not in SKIP_TYPES and m["point"] != "/"]
    with open("/proc/net/dev") as f:
        interfaces = [line.split(":")[0].strip() for line in f.readlines()[2:]]
    with open("/proc/net/unix") as f:
        unix = [line.split()[-1] for line in f.readlines()[1:] if len(line.split()) > 7]
    report = {
        "mounts": seen,
        "canaries": canaries(walked, asked.get("canaries", [])),
        "interfaces": interfaces,
        "unix": unix,
        "tries": tries(asked.get("tries", [])),
        "writable": writable(
            sorted({*walked, "/", "/etc", "/usr", "/opt", "/cache/m2", "/var/tmp", "/dev/mqueue"})
        ),
        "resolv": read("/etc/resolv.conf"),
        "status": status(),
        "processes": processes(),
        "environment": sorted(os.environ),
        "sockets_anywhere": [
            os.path.join(d, n)
            for top in walked
            for d, _, files in os.walk(top)
            for n in files
            if os.path.exists(os.path.join(d, n))
            and os.stat(os.path.join(d, n), follow_symlinks=False).st_mode & 0o170000 == 0o140000
        ],
    }
    print(json.dumps(report))


if __name__ == "__main__":
    main()
