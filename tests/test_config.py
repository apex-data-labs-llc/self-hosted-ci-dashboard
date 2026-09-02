import json

import pytest

from config import DEFAULTS, load_config


def _write(tmp_path, data):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_load_config_missing_file_raises_with_helpful_message(tmp_path):
    with pytest.raises(ValueError) as exc_info:
        load_config(tmp_path / "does-not-exist.json")
    assert "not found" in str(exc_info.value)
    assert "config.example.json" in str(exc_info.value)


def test_load_config_requires_owner_and_repos(tmp_path):
    with pytest.raises(ValueError, match="owner"):
        load_config(_write(tmp_path, {"repos": ["hello-world"]}))
    with pytest.raises(ValueError, match="repos"):
        load_config(_write(tmp_path, {"owner": "octocat"}))
    with pytest.raises(ValueError, match="repos"):
        load_config(_write(tmp_path, {"owner": "octocat", "repos": []}))


def test_load_config_merges_over_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, {"owner": "octocat", "repos": ["hello-world"]}))
    assert cfg["owner"] == "octocat"
    assert cfg["repos"] == ["hello-world"]
    assert cfg["port"] == DEFAULTS["port"]
    assert cfg["restart_command"] is None
    assert cfg["local_probe"] is None


def test_load_config_rejects_malformed_json(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_config(p)


def test_load_config_rejects_local_probe_missing_required_keys(tmp_path):
    with pytest.raises(ValueError, match="local_probe"):
        load_config(_write(tmp_path, {
            "owner": "octocat", "repos": ["hello-world"],
            "local_probe": {"command": ["true"]},  # missing "pattern"
        }))


def test_load_config_accepts_well_formed_local_probe_and_restart_command(tmp_path):
    cfg = load_config(_write(tmp_path, {
        "owner": "octocat", "repos": ["hello-world"],
        "local_probe": {"command": ["true"], "pattern": r"(\w+)"},
        "restart_command": ["true"],
    }))
    assert cfg["local_probe"]["command"] == ["true"]
    assert cfg["restart_command"] == ["true"]


def test_load_config_rejects_non_list_restart_command(tmp_path):
    with pytest.raises(ValueError, match="restart_command"):
        load_config(_write(tmp_path, {
            "owner": "octocat", "repos": ["hello-world"],
            "restart_command": "not-a-list",
        }))


def test_example_config_is_valid():
    # config.example.json ships committed and must itself load cleanly -- it is what a
    # new user copies to config.json.
    import pathlib
    example = pathlib.Path(__file__).resolve().parent.parent / "config.example.json"
    cfg = load_config(example)
    assert cfg["owner"] == "octocat"
    assert set(cfg["repos"]) == {"hello-world", "spoon-knife"}
    # The example ships local_probe/restart_command disabled (null) by default, with
    # the platform-specific shapes documented under separate "_..._example" keys that
    # load_config ignores (unknown keys pass through harmlessly).
    assert cfg["local_probe"] is None
    assert cfg["restart_command"] is None
