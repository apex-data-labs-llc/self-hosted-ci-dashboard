"""In-process tests for server.py internals that are hard to reach through HTTP:
the collector loop's failure handling, the log-tail cache, partial-pass merging, and
pass scheduling."""
import importlib
import pathlib
import sys
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("CI_DASH_CONFIG", str(ROOT / "tests" / "fixtures" / "test_config.json"))
    monkeypatch.setenv("CI_DASH_LOG_PATH", str(tmp_path / "actions.log"))
    monkeypatch.setenv("CI_DASH_NO_GH_STATUS", "1")
    sys.modules.pop("server", None)
    mod = importlib.import_module("server")
    yield mod
    sys.modules.pop("server", None)


def test_collector_loop_survives_an_exception(server, monkeypatch):
    def boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(server, "_collector_tick", boom)
    t = threading.Thread(target=server.collector_loop, daemon=True)
    t.start()
    for _ in range(50):
        if server.STATE.last_pass_error:
            break
        time.sleep(0.02)
    assert t.is_alive()
    assert "boom" in server.STATE.last_pass_error
    # Rescheduled a full interval out, not a hot retry loop against the API.
    assert server.STATE.next_pass_at > time.time() + server.INTERVAL_S - 5


def test_restart_driver_failure_still_publishes_the_snapshot(server, monkeypatch):
    fresh = {"fetched_at": "2026-09-25T00:00:00+00:00", "banner": None, "repos": {}, "rate_limit": {},
             "api_calls": 3, "error_kinds": []}
    monkeypatch.setattr(server.C, "build_snapshot", lambda *a, **k: dict(fresh))

    def bad_restart(snap, now):
        raise RuntimeError("restart exploded")
    monkeypatch.setattr(server, "drive_restart", bad_restart)
    snap = server.run_pass()
    assert snap is not None
    assert server.STATE.snapshot["fetched_at"] == fresh["fetched_at"]
    assert "restart exploded" in server.STATE.snapshot["banner"]
    assert server.STATE.pass_running is False and server.STATE.last_pass_ok_at is not None


def test_log_tail_is_cached_only_for_completed_jobs(server, monkeypatch):
    calls = []
    monkeypatch.setattr(server.G, "gh_text", lambda args, timeout: calls.append(args) or "l1\nl2")
    server.STATE.snapshot = {"repos": {"hello-world": {"prs": [{"runs": [{"jobs": [
        {"job_id": 1, "status": "completed"}, {"job_id": 2, "status": "in_progress"}]}]}]}}}
    assert server.fetch_log_tail("hello-world", "1") == "l1\nl2"
    server.fetch_log_tail("hello-world", "1")
    assert len(calls) == 1
    server.fetch_log_tail("hello-world", "2")
    server.fetch_log_tail("hello-world", "2")
    assert len(calls) == 3, "an in-progress job's log is still growing and must not be cached"


def test_log_cache_is_bounded(server):
    assert server.STATE.log_cache.limit == server.LOG_CACHE_LIMIT


def test_merge_partial_keeps_full_pass_fields(server):
    prev = {"fetched_at": "full", "api_calls": 40, "partial": False,
            "repos": {"hello-world": {"x": 1}, "spoon-knife": {"y": 2}}}
    part = {"fetched_at": "later", "api_calls": 6, "rate_limit": {"remaining": 9}, "banner": None,
            "repos": {"hello-world": {"x": 3, "error_kinds": ["timeout"]}}}
    merged = server._merge_partial(prev, part)
    assert merged["fetched_at"] == "full" and merged["api_calls"] == 40
    assert merged["repos"] == {"hello-world": {"x": 3, "error_kinds": ["timeout"]}, "spoon-knife": {"y": 2}}
    assert merged["partial_pass_at"] == "later" and merged["partial_pass_api_calls"] == 6
    assert merged["error_kinds"] == ["timeout"] and merged["rate_limit"] == {"remaining": 9}


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_webhooks_missed_needs_evidence(server, monkeypatch):
    monkeypatch.setattr(server, "WEBHOOK_SECRET", "s")
    now = time.time()
    server.STATE.webhooks["last_at"] = now - 3600
    snap = {"repos": {"hello-world": {"latest_run_created_at": _iso(now - 1800)}}}
    assert "no webhook" in server._check_webhooks_missed(snap, now)
    snap = {"repos": {"hello-world": {"latest_run_created_at": _iso(now - 3600 + 60)}}}
    assert server._check_webhooks_missed(snap, now) is None  # within grace
    server.STATE.webhooks["last_at"] = None
    assert server._check_webhooks_missed({"repos": {}}, now) is None  # never received: no claim


def test_schedule_uses_sweep_interval_only_while_webhooks_are_trusted(server, monkeypatch):
    snap = {"rate_limit": {}, "api_calls": 1, "error_kinds": []}
    server._schedule_next(snap)
    assert abs(server.STATE.next_pass_at - (time.time() + server.INTERVAL_S)) < 5
    monkeypatch.setattr(server, "WEBHOOK_SECRET", "s")
    server.STATE.webhooks.update(last_at=time.time(), missed=None)
    server._schedule_next(snap)
    assert abs(server.STATE.next_pass_at - (time.time() + max(server.INTERVAL_S, server.WEBHOOK_SWEEP_S))) < 5
    server.STATE.webhooks["missed"] = "evidence"
    server._schedule_next(snap)
    assert abs(server.STATE.next_pass_at - (time.time() + server.INTERVAL_S)) < 5
