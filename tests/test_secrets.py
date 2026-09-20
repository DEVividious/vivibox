import stat

from vivibox import secrets


def test_prepare_writes_private_files_and_keeps_password(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    d = secrets.prepare("demo-1", ["deepseek"], get_key=lambda p: f"key-for-{p}")
    assert (d / "deepseek").read_text() == "key-for-deepseek"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "deepseek").stat().st_mode) == 0o600
    password = (d / "server-password").read_text()
    secrets.prepare("demo-1", ["deepseek"], get_key=lambda p: "rotated")
    assert (d / "server-password").read_text() == password
    assert (d / "deepseek").read_text() == "rotated"
    secrets.remove("demo-1")
    assert not d.exists()


def test_only_the_needed_keys_are_copied(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    d = secrets.prepare("demo-1", ["deepseek"], get_key=lambda p: "k")
    assert sorted(p.name for p in d.iterdir()) == ["deepseek", "server-password"]
