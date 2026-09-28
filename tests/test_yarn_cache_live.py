"""Downloaded Yarn packages survive a fresh verification; installed files never do."""

import json
import subprocess

import pytest
from test_pod_live import agent, gate
from test_pod_live import env as pod_environment

pytestmark = pytest.mark.docker
env = pod_environment


@pytest.mark.parametrize(
    ("version", "local"), [("1.22.22", False), ("3.8.7", False), ("4.9.2", False), ("4.9.2", True)]
)
def test_yarn_reuses_downloads_after_recreating_the_gate(env, version, local):
    pod = env["pod"]
    repo = env["repo"]
    package = repo / "package.json"
    rc = repo / ".yarnrc.yml"
    package.write_text(
        json.dumps(
            {
                "name": "cache-check",
                "private": True,
                "packageManager": f"yarn@{version}",
                "dependencies": {"is-number": "7.0.0"},
            }
        )
    )
    if not version.startswith("1."):
        rc.write_text(
            "nodeLinker: node-modules\n"
            + ("enableGlobalCache: false\ncacheFolder: .yarn/cache\n" if local else "")
        )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", f"Use Yarn {version}"], cwd=repo, check=True, capture_output=True)
    try:
        pod.gate_up()
        if version.startswith("1."):
            cache = gate(env, "yarn cache dir").stdout.strip()
            assert cache.startswith("/cache/yarn/"), cache
        else:
            global_dir = gate(env, "yarn config get globalFolder").stdout.strip()
            assert global_dir == "/cache/yarn/berry", global_dir
            if local:
                cache = gate(env, "yarn config get cacheFolder").stdout.strip()
                assert cache == f"{pod.gate_src}/.yarn/cache", "keep a project's local-cache choice"
        gate(env, "yarn install")
        lock = gate(env, "cat yarn.lock").stdout
        assert gate(env, "node -e 'console.log(require(\"is-number\")(42))'").stdout.strip() == "true"
        gate(env, "touch node_modules/first-build /cache/m2/installed/first-build")
        gate(env, "touch /cache/build-cache/first-build")
        assert agent(env, "test -e /cache/build-cache/first-build", check=False).returncode != 0
        assert agent(env, "test -e /cache/m2/installed/first-build", check=False).returncode != 0
        # Only the lockfile, never node_modules, is part of the next verification's commit.
        (repo / "yarn.lock").write_text(lock)
        subprocess.run(["git", "add", "yarn.lock"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-qm", "Lock the dependencies"], cwd=repo, check=True, capture_output=True
        )
        pod.gate_up()
        assert gate(env, "test -e node_modules", check=False).returncode != 0
        assert gate(env, "test -e /cache/m2/installed/first-build", check=False).returncode != 0
        assert gate(env, "test -e /cache/build-cache/first-build", check=False).returncode == 0
        command = (
            "yarn install --offline --frozen-lockfile"
            if version.startswith("1.")
            else "YARN_ENABLE_NETWORK=0 yarn install --immutable"
        )
        gate(env, command)
        assert gate(env, "node -e 'console.log(require(\"is-number\")(42))'").stdout.strip() == "true"
        assert gate(env, "test -e node_modules/first-build", check=False).returncode != 0
    finally:
        pod.gate_down()
        for path in (package, rc, repo / "yarn.lock"):
            path.unlink(missing_ok=True)
        subprocess.run(["git", "add", "-u"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-qm", "Remove the Yarn fixture"], cwd=repo, check=True, capture_output=True
        )


def test_classic_keeps_a_projects_explicit_cache_folder(env):
    pod = env["pod"]
    pod.gate_up()
    try:
        gate(env, "printf 'cache-folder \"./local-cache\"\\n' > .yarnrc")
        found = gate(env, "corepack yarn@1.22.22 cache dir").stdout.strip()
        assert found == f"{pod.gate_src}/local-cache/v6", found
    finally:
        pod.gate_down()


def test_deleting_one_pod_keeps_shared_cache_and_another_live_pod(env):
    from vivibox.pod import Pod

    parent = env["pod"]
    pod = Pod("delete-" + parent.task_id, parent.repo, parent.image, [], gate_dir=parent.gate_dir / "delete")
    marker = f"/cache/yarn/{pod.task_id}"
    try:
        pod.up()
        pod.gate_up()
        pod.gate_exec("touch", marker)
        pod.gate_exec("touch", "/cache/m2/installed/from-this-verification")
        pod.gate_exec("touch", "/cache/build-cache/from-this-verification")
        pod.remove()
        for kind, names in (
            ("container", [pod.agent, pod.sidecar, pod.gate, pod.review]),
            ("volume", [*pod.volumes.values(), f"vivibox-{pod.task_id}-config"]),
            ("network", [pod.network]),
        ):
            for name in names:
                result = subprocess.run(["docker", kind, "inspect", name], capture_output=True)
                assert result.returncode != 0, f"{kind} {name} left behind"
        assert parent.exec("test", "-f", marker).returncode == 0
        assert parent.exec("docker", "info").returncode == 0, "the other pod still works"
    finally:
        pod.remove()
        parent.exec("rm", "-f", marker, check=False)
