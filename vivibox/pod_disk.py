"""A task's disk: an image file in the task's folder, attached by the root helper
(host/vivibox-host) as a loop device mounted at <folder>/disk, where the pod's volumes are
directories (pod.Pod.volumes). Its size is the most the task writes outside its clone: the pod's
Docker data, the agent's home, what a build installs. The helper is given the task's own folder
and the configured size, nothing from Docker's output (invariant 13). Mixed into Pod, whose
task_id, folder, limits and runner it uses."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from .pod_egress import HELPER, helper
from .podspec import size_option

DISK_IMAGE = "disk.img"
DISK_MOUNT = "disk"
# What the helper makes on the disk, owned by the task's owner (host/vivibox-host: the same).
DISK_DIRS = ("docker", "config", "installed", "gate-build-cache", "gate", "review")


class PodDisk:
    @property
    def disk(self) -> Path:
        """Where the task's disk is mounted."""
        return self.folder / DISK_MOUNT

    @property
    def disk_image(self) -> Path:
        return self.folder / DISK_IMAGE

    def _disk_request(self, size: str = "") -> str:
        spec = {"dir": str(self.folder)}
        if size:
            spec["size"] = size
        return json.dumps(spec)

    def attach_disk(self) -> None:
        """The disk mounted, made first when the task has none, at the configured size: the pod
        does not start without it."""
        helper(
            self.runner, "attach-disk", self.task_id, self._disk_request(size_option(self.limits.disk)),
            what="the task's disk could not be attached",
        )  # fmt: skip

    def grow_disk(self, size: str) -> None:
        """A larger disk, the task's attached first (a restart of the machine detaches every
        disk); the helper refuses to shrink."""
        self.attach_disk()
        helper(
            self.runner, "grow-disk", self.task_id, self._disk_request(size),
            what="the task's disk could not be grown",
        )  # fmt: skip

    def detach_disk(self) -> str:
        """The disk unmounted and its loop device freed; "" when it is, else what the helper said.
        A task that never had one is detached already, the helper says."""
        p = self.runner(["sudo", "-n", HELPER, "detach-disk", self.task_id, self._disk_request()])
        if p.returncode == 0:
            return ""
        said = (p.stderr or p.stdout).strip()[-400:]
        return (
            f"the task's disk at {self.disk} was not detached: {said}. Its image {self.disk_image} is "
            "kept; host/setup.sh installs the helper if it is missing"
        )

    def remove_disk(self) -> str:
        """The disk detached and its image deleted, as the person, after a detach that worked: a
        mounted image is never deleted. Returns what the host kept, "" when nothing."""
        if left := self.detach_disk():
            return left
        with contextlib.suppress(OSError):
            os.unlink(self.disk_image)
        with contextlib.suppress(OSError):
            os.rmdir(self.disk)
        return ""
