import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEST_CONFIG = ROOT / "tests" / "fixtures" / "test_config.json"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _start(tmp_path, delay="0", restart_cmd="python -c pass"):
    port = _free_port()
    # Defect fix: the real server writes restart audit entries to actions.log next to
    # server.py. Tests must never write to that production file -- CI_DASH_LOG_PATH
    # points every test server at a throwaway path instead.
    log_path = tmp_path / "actions.log"
    env = {**os.environ,
           "PATH": str(ROOT / "tests" / "fake_gh") + os.pathsep + os.environ["PATH"],
           "FAKE_GH_DELAY": delay, "CI_DASH_PORT": str(port), "CI_DASH_INTERVAL": "3600",
           "CI_DASH_NO_LOCAL_PROBE": "1", "CI_DASH_CONFIG": str(TEST_CONFIG),
           "CI_DASH_LOG_PATH": str(log_path)}
    if restart_cmd is not None:
        env["CI_DASH_RESTART_CMD"] = restart_cmd
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1); break
        except Exception:
            time.sleep(0.1)
    return proc, port, log_path


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, json.loads(r.read())


def _post(port, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST",
                                 data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# The fake `gh` shim in tests/fake_gh spawns a real OS process per call, and a single
# collector pass makes many of them (one per PR head-sha run query, one per pending
# run's jobs fetch). Per-process spawn cost is noisy on a real filesystem (antivirus
# scanning and general system load), so an end-to-end fake pass can take anywhere from
# a few seconds to tens of seconds. Poll generously rather than pick a tight fixed
# timeout that would make genuinely-passing behavior flake -- this does not loosen what
# is being asserted (the poll loop still waits for the real, specific condition and the
# test still fails if it's never met).
_POLL_ITERATIONS = 400
_POLL_INTERVAL_S = 0.25


def test_status_is_instant_during_a_slow_pass(tmp_path):
    proc, port, _ = _start(tmp_path, delay="2")
    try:
        t = time.time()
        status, body = _get(port, "/api/status")
        assert status == 200 and time.time() - t < 1.0
        assert "repos" in body
    finally:
        proc.kill()


def test_status_eventually_has_prs_and_refresh_coalesces(tmp_path):
    proc, port, _ = _start(tmp_path)
    try:
        _post(port, "/api/refresh"); _post(port, "/api/refresh")
        for _ in range(_POLL_ITERATIONS):
            _, body = _get(port, "/api/status")
            if body["repos"]["hello-world"].get("prs"):
                break
            time.sleep(_POLL_INTERVAL_S)
        assert body["repos"]["hello-world"]["prs"]
        assert body["restart"]["state"] == "idle"
        assert body["owner"] == "octocat"
    finally:
        proc.kill()


def test_restart_endpoint_validates_and_logs(tmp_path):
    proc, port, log_path = _start(tmp_path)
    try:
        for _ in range(_POLL_ITERATIONS):
            _, body = _get(port, "/api/status")
            if body["repos"]["hello-world"].get("runners"):
                break
            time.sleep(_POLL_INTERVAL_S)
        status, body = _post(port, "/api/restart", {"mode": "idle"})
        assert status == 202 and body["state"] in ("draining", "restarting", "arming")
        assert log_path.exists(), "restart log must be written to the configured (temp) path"
        log = log_path.read_text()
        assert "restart_requested" in log
    finally:
        proc.kill()
    # And it must NOT have touched the real project-root actions.log.
    real_log = ROOT / "actions.log"
    assert not real_log.exists() or "restart_requested" not in real_log.read_text()


def test_restart_endpoint_rejects_bad_mode(tmp_path):
    proc, port, _ = _start(tmp_path)
    try:
        status, body = _post(port, "/api/restart", {"mode": "bogus"})
        assert status == 400
        assert "bogus" in body["error"]
    finally:
        proc.kill()


def test_restart_disabled_when_not_configured_hides_from_snapshot_and_refuses(tmp_path):
    # No restart_command in config.json and no CI_DASH_RESTART_CMD override: the
    # feature must be entirely off -- snapshot says so, and the endpoint refuses with a
    # clear 400 rather than silently doing nothing or crashing.
    proc, port, _ = _start(tmp_path, restart_cmd=None)
    try:
        for _ in range(_POLL_ITERATIONS):
            _, body = _get(port, "/api/status")
            if body.get("fetched_at"):
                break
            time.sleep(_POLL_INTERVAL_S)
        assert body["restart_enabled"] is False
        status, body = _post(port, "/api/restart", {"mode": "idle"})
        assert status == 400
        assert "not configured" in body["error"]
    finally:
        proc.kill()


def test_restart_enabled_reflected_in_snapshot_when_configured(tmp_path):
    proc, port, _ = _start(tmp_path, restart_cmd="python -c pass")
    try:
        for _ in range(_POLL_ITERATIONS):
            _, body = _get(port, "/api/status")
            if body.get("fetched_at"):
                break
            time.sleep(_POLL_INTERVAL_S)
        assert body["restart_enabled"] is True
    finally:
        proc.kill()
