"""What a live task's pod uses now: CPU and memory per container, disk per volume and folder."""

import json

from vivibox import resources

STATS = "\n".join(
    json.dumps(row)
    for row in (
        {"Name": "vivibox-demo-1-agent", "CPUPerc": "12.50%", "MemUsage": "512MiB / 15.5GiB"},
        {"Name": "vivibox-demo-1-dind", "CPUPerc": "0.40%", "MemUsage": "80.2MiB / 15.5GiB"},
        {"Name": "vivibox-demo-12-agent", "CPUPerc": "1.00%", "MemUsage": "1GiB / 15.5GiB"},
        {"Name": "postgres", "CPUPerc": "3.00%", "MemUsage": "100MiB / 15.5GiB"},
    )
)
DF = json.dumps(
    {
        "Volumes": [
            {"Name": "vivibox-demo-1-docker", "Size": "1.5GB"},
            {"Name": "vivibox-demo-1-installed", "Size": "20.5MB"},
            {"Name": "vivibox-demo-1-gate-build-cache", "Size": "0B"},
            {"Name": "vivibox-demo-12-docker", "Size": "9GB"},
            {"Name": "vivibox-cache-m2", "Size": "269.8MB"},
        ]
    }
)


def test_sizes_read_as_docker_writes_them():
    assert resources.parse_size("0B") == 0
    assert resources.parse_size("512MiB") == 512 * 2**20
    assert resources.parse_size("1.5GB") == 1_500_000_000
    assert resources.parse_size("20.5kB") == 20_500
    assert resources.parse_size("--") == 0


def test_a_sample_is_per_task_and_per_container(tmp_path):
    calls = []

    def run(command):
        calls.append(command[:3])
        if command[:2] == ["docker", "stats"]:
            return STATS
        if command[:3] == ["docker", "system", "df"]:
            return DF
        return f"{2048}\t{command[-1]}\n"  # du -sk: kilobytes

    found = resources.sample({"demo-1": tmp_path / "demo-1", "demo-12": tmp_path / "demo-12"}, run=run)
    one = found["demo-1"]
    assert [(c.role, c.cpu) for c in one.containers] == [("agent", 12.5), ("dind", 0.4)]
    assert one.memory == 512 * 2**20 + int(80.2 * 2**20)
    assert one.volumes == {"docker": 1_500_000_000, "installed": 20_500_000, "gate-build-cache": 0}
    assert one.files == 2048 * 1024 and one.disk == 1_520_500_000 + 2048 * 1024
    assert [c.role for c in found["demo-12"].containers] == ["agent"], "demo-1's are not demo-12's"
    assert sum(1 for c in calls if c[:2] == ["docker", "stats"]) == 1, "one docker stats for every task"


def test_docker_not_answering_leaves_the_figures_out(tmp_path):
    def run(command):
        raise resources.Unavailable("docker: not found")

    problems: list[str] = []
    assert resources.sample({"demo-1": tmp_path}, run=run, problems=problems) == {}
    assert problems == ["Docker did not answer: docker: not found"], "why, for the screen to say"


def test_sizes_for_a_person():
    assert [resources.size(n) for n in (0, 900, 20_500_000, 3 * 2**30)] == ["-", "900 B", "20 MB", "3.2 GB"]


def test_shared_caches_are_measured_once_even_without_live_tasks(tmp_path):
    calls = []

    def run(command):
        calls.append(command)
        if command[:3] == ["docker", "system", "df"]:
            data = json.loads(DF)
            data["Volumes"] += [
                {"Name": "vivibox-cache-yarn", "Size": "100MB"},
                {"Name": "some-other-cache", "Size": "9GB"},
            ]
            return json.dumps(data)
        return ""

    shared = {}
    found = resources.sample({}, run=run, shared_caches=shared)
    assert found == {}
    assert shared == {"m2": 269_800_000, "yarn": 100_000_000}
    assert len([c for c in calls if c[:3] == ["docker", "system", "df"]]) == 1

    found = resources.sample({"demo-1": tmp_path, "demo-12": tmp_path}, run=run, shared_caches=shared)
    assert found["demo-1"].disk == 1_520_500_000
    assert found["demo-12"].disk == 9_000_000_000
    assert sum(shared.values()) == 369_800_000
