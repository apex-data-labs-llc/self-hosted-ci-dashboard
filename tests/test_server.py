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


def _start(tmp_path, delay="0", restart_cmd="python -c pass", extra_env=None):
    port = _free_port()
    # Defect fix: the real server writes restart audit entries to actions.log next to
    # server.py. Tests must never write to that production file -- CI_DASH_LOG_PATH
    # points every test server at a throwaway path instead.
    log_path = tmp_path / "actions.log"
    env = {**os.environ,
           "PATH": str(ROOT / "tests" / "fake_gh") + os.pathsep + os.environ["PATH"],
           "FAKE_GH_DELAY": delay, "CI_DASH_PORT": str(port), "CI_DASH_INTERVAL": "3600",
           "CI_DASH_NO_LOCAL_PROBE": "1", "CI_DASH_CONFIG": str(TEST_CONFIG),
           "CI_DASH_LOG_PATH": str(log_path), "CI_DASH_NO_GH_STATUS": "1"}
    if restart_cmd is not None:
        env["CI_DASH_RESTART_CMD"] = restart_cmd
    for k, v in (extra_env or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
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


def _wait_for(port, pred):
    body = None
    for _ in range(_POLL_ITERATIONS):
        _, body = _get(port, "/api/status")
        if pred(body):
            return body
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(f"condition never met; last status: {body}")


def _post_raw(port, path, data, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_refresh_is_debounced_after_a_pass(tmp_path):
    proc, port, _ = _start(tmp_path, extra_env={"CI_DASH_MIN_REFRESH": "60"})
    try:
        _wait_for(port, lambda b: b.get("fetched_at") and not b["collector"]["pass_running"])
        status, body = _post(port, "/api/refresh")
        assert status == 429
        assert 0 < body["retry_after"] <= 60
    finally:
        proc.kill()


def test_status_reports_collector_health_calls_and_live_next_pass(tmp_path):
    proc, port, _ = _start(tmp_path)
    try:
        body = _wait_for(port, lambda b: b.get("fetched_at") and not b["collector"]["pass_running"])
        assert body["api_calls"] > 0
        assert body["collector"]["alive"] is True and body["collector"]["stale"] is False
        # next_pass_at is the live schedule, not the (already past) value from pass start.
        assert body["next_pass_at"] > body["served_at"]
        status, health = _get(port, "/api/health")
        assert status == 200 and health["ok"] is True
        assert body["gh_health"]["enabled"] is False  # CI_DASH_NO_GH_STATUS in tests
    finally:
        proc.kill()


def test_webhook_endpoint_is_off_without_a_secret(tmp_path):
    proc, port, _ = _start(tmp_path)
    try:
        status, body = _post_raw(port, "/api/webhook", b"{}", {"X-GitHub-Event": "ping"})
        assert status == 404 and "not configured" in body["error"]
    finally:
        proc.kill()


def test_signed_webhook_triggers_a_partial_pass_and_bad_signature_is_rejected(tmp_path):
    import hashlib
    import hmac
    secret = "s3cret"
    proc, port, _ = _start(tmp_path, extra_env={"CI_DASH_WEBHOOK_SECRET": secret,
                                                "CI_DASH_WEBHOOK_COALESCE": "0", "CI_DASH_MIN_REFRESH": "0"})
    try:
        _wait_for(port, lambda b: b.get("fetched_at") and not b["collector"]["pass_running"])
        payload = json.dumps({"action": "requested",
                              "repository": {"name": "hello-world", "owner": {"login": "octocat"}}}).encode()
        status, _ = _post_raw(port, "/api/webhook", payload,
                              {"X-GitHub-Event": "workflow_run", "X-Hub-Signature-256": "sha256=" + "0" * 64})
        assert status == 401
        status, _ = _post_raw(port, "/api/webhook", payload, {"X-GitHub-Event": "workflow_run"})
        assert status == 401
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        status, body = _post_raw(port, "/api/webhook", payload,
                                 {"X-GitHub-Event": "workflow_run", "X-Hub-Signature-256": sig})
        assert status == 202 and body["refresh"] == "hello-world"
        body = _wait_for(port, lambda b: b.get("partial_pass_at"))
        assert body["webhooks"]["received"] == 1 and body["webhooks"]["rejected"] == 2
        assert body["repos"]["hello-world"]["fetched_at"] == body["partial_pass_at"]
        assert body["repos"]["spoon-knife"]["prs"], "the other repo keeps its full-pass data"
    finally:
        proc.kill()


def test_github_status_feed_is_read_and_served(tmp_path):
    url = (ROOT / "tests" / "fixtures" / "gh_status_summary.json").as_uri()
    proc, port, _ = _start(tmp_path, extra_env={"CI_DASH_NO_GH_STATUS": None, "CI_DASH_GH_STATUS_URL": url})
    try:
        body = _wait_for(port, lambda b: b["gh_health"].get("components"))
        comps = {c["name"]: c["status"] for c in body["gh_health"]["components"]}
        assert comps["Actions"] == "partial_outage"
        assert body["gh_health"]["stale"] is False
    finally:
        proc.kill()


def test_unreachable_github_status_feed_reads_unknown_not_green(tmp_path):
    proc, port, _ = _start(tmp_path, extra_env={"CI_DASH_NO_GH_STATUS": None,
                                                "CI_DASH_GH_STATUS_URL": "http://127.0.0.1:9/nothing"})
    try:
        body = _wait_for(port, lambda b: b["gh_health"].get("error") not in (None, "not run yet"))
        assert body["gh_health"]["stale"] is True
        assert body["gh_health"]["components"] is None
    finally:
        proc.kill()


def test_webhook_rejects_malformed_requests(tmp_path):
    proc, port, _ = _start(tmp_path, extra_env={"CI_DASH_WEBHOOK_SECRET": "s3cret"})
    try:
        status, _ = _post_raw(port, "/api/webhook", b"{}", {"X-GitHub-Event": "ping",
                                                           "X-Hub-Signature-256": "sha256=é"})
        assert status == 401
    finally:
        proc.kill()
