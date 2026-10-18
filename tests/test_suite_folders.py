import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
# What conftest.py makes in the system's temporary directory, one of each per process.
SUITES_OWN = ("vivibox-tests-home-", "vivibox-tests-tmp-")
TWO_TESTS = [
    "tests/test_image.py::test_build_command_passes_user",
    "tests/test_image.py::test_digest_depends_on_user_and_context",
]


@pytest.mark.parametrize("fails", [False, True], ids=["passed", "failed"])
def test_a_run_leaves_none_of_the_suites_folders_in_the_temporary_directory(tmp_path, fails):
    """conftest.py makes a home and a temporary folder of the suite's own in the system's
    temporary directory, in every process of a run: runs with -n auto had left 4010 of them in
    /tmp. They go as the run ends, whether its tests passed or not; nothing else there is touched."""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "made_to_fail.py").write_text("def pytest_runtest_call(item):\n    raise AssertionError\n")
    system_tmp = tmp_path / "tmp"
    system_tmp.mkdir()
    (system_tmp / "vivibox-tests-home-not-this-runs").mkdir()
    env = {**os.environ, "TMPDIR": str(system_tmp), "PYTHONPATH": str(plugins)}
    command = [sys.executable, "-m", "pytest", "-q", "-n", "2", "-p", "no:cacheprovider", *TWO_TESTS]
    command += ["-p", "made_to_fail"] * fails
    run = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert run.returncode == (1 if fails else 0), run.stdout[-3000:] + run.stderr[-3000:]
    left = sorted(p.name for p in system_tmp.iterdir() if p.name.startswith(SUITES_OWN))
    assert left == ["vivibox-tests-home-not-this-runs"], "only another run's folder stays"
