from pathlib import Path

from settings import load_settings


def test_example_config(tmp_path: Path):
    (tmp_path / "config.example.ini").write_text("[server]\nport = 8080\n")
    assert load_settings(tmp_path)["server"]["port"] == "8080"
