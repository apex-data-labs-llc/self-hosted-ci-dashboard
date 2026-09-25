"""Loads config.json (see config.example.json), merged over defaults.

config.json itself is gitignored -- copy config.example.json to config.json and edit it.
Only `owner` and `repos` are required; everything else has a working default. Absent
`local_probe` disables the local-process probe entirely (every runner's `local_alive`
stays null/unknown, never a fabricated "dead"). Absent `restart_command` disables the
runner-restart feature entirely (the UI hides the buttons; the endpoint refuses with 400).
"""
import json
import pathlib

DEFAULTS = {
    "owner": None,
    "repos": [],
    "deploy_workflows": {},
    "runner_local_dirs": {},
    "local_probe": None,
    "restart_command": None,
    "port": 8787,
    "poll_seconds": 300,
    "pass_budget_seconds": 150,
    "jobs_pool_workers": 8,
    "gh_timeout_seconds": 20,
    "actions_log_path": "actions.log",
    "min_refresh_seconds": 30,
    "sha_cache_ttl_seconds": 900,
    "status_poll_seconds": 120,
    "gh_status_components": None,
    "webhook_secret": None,
    "webhook_sweep_seconds": 1800,
}

REQUIRED_LOCAL_PROBE_KEYS = ("command", "pattern")


def load_config(path):
    """Read and validate one config file. Raises ValueError with a message naming
    exactly what's wrong, rather than silently falling back to guessed values."""
    path = pathlib.Path(path)
    if not path.exists():
        raise ValueError(
            f"config file not found: {path} -- copy config.example.json to config.json and edit it"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"config file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must contain a JSON object")

    cfg = dict(DEFAULTS)
    cfg.update(data)

    if not cfg.get("owner") or not isinstance(cfg["owner"], str):
        raise ValueError(f"config {path}: 'owner' is required and must be a non-empty string")
    if not cfg.get("repos") or not isinstance(cfg["repos"], list):
        raise ValueError(f"config {path}: 'repos' is required and must be a non-empty list")

    probe = cfg.get("local_probe")
    if probe is not None:
        if not isinstance(probe, dict) or any(k not in probe for k in REQUIRED_LOCAL_PROBE_KEYS):
            raise ValueError(
                f"config {path}: 'local_probe', when set, must be an object with "
                f"'command' (a list) and 'pattern' (a regex string)"
            )

    restart_cmd = cfg.get("restart_command")
    if restart_cmd is not None and not isinstance(restart_cmd, list):
        raise ValueError(f"config {path}: 'restart_command', when set, must be a list of argv strings")

    comps = cfg.get("gh_status_components")
    if comps is not None and (not isinstance(comps, list) or not all(isinstance(c, str) for c in comps)):
        raise ValueError(f"config {path}: 'gh_status_components', when set, must be a list of strings")

    secret = cfg.get("webhook_secret")
    if secret is not None and (not isinstance(secret, str) or not secret):
        raise ValueError(f"config {path}: 'webhook_secret', when set, must be a non-empty string")

    for key in ("min_refresh_seconds", "sha_cache_ttl_seconds", "status_poll_seconds", "webhook_sweep_seconds"):
        if not isinstance(cfg.get(key), int) or isinstance(cfg.get(key), bool) or cfg[key] < 0:
            raise ValueError(f"config {path}: '{key}' must be a non-negative integer")

    return cfg
