#!/usr/bin/env python3
"""Self-hosted GitHub Actions CI dashboard server.

Thread model:
  - HTTP handler thread(s) (ThreadingHTTPServer) only ever read the last completed
    snapshot under STATE.lock. They never call build_snapshot themselves, so a slow
    pass (a full pass against a busy org can take roughly a minute) can never make
    /api/status block.
  - collector_loop() runs on its own daemon thread, calling build_snapshot() on a
    timer (or when woken by /api/refresh), and swaps STATE.snapshot in one lock
    acquisition when done.
  - local_probe_loop() runs on its own daemon thread with its own timeout, independent
    of the collector. A wedged probe command can only ever stall its own thread. It is
    a no-op loop (never runs the configured command) unless config.json sets
    `local_probe`.
"""
import http.server
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import collector as C
import gh_client as G
import restart as R
from config import load_config

BASE_DIR = pathlib.Path(__file__).resolve().parent

CONFIG_PATH = pathlib.Path(os.environ.get("CI_DASH_CONFIG", BASE_DIR / "config.json"))
CFG = load_config(CONFIG_PATH)

PORT = int(os.environ.get("CI_DASH_PORT", CFG["port"]))
INTERVAL_S = int(os.environ.get("CI_DASH_INTERVAL", CFG["poll_seconds"]))
LOCAL_PROBE_INTERVAL_S = int(os.environ.get("CI_DASH_PROBE_INTERVAL", "60"))
LOCAL_PROBE_TIMEOUT_S = 10
NO_LOCAL_PROBE = os.environ.get("CI_DASH_NO_LOCAL_PROBE") == "1"

# Wire collector's module-level constants from config. Done once, at import time,
# before any collector call happens.
C.OWNER = CFG["owner"]
C.REPOS = list(CFG["repos"])
C.DEPLOY_WORKFLOW_PATHS = {repo: set(paths) for repo, paths in CFG.get("deploy_workflows", {}).items()}
C.RUNNER_LOCAL_DIRS = {
    (repo, runner): local_dir
    for repo, mapping in (CFG.get("runner_local_dirs") or {}).items()
    for runner, local_dir in mapping.items()
}
C.PASS_BUDGET_S = CFG.get("pass_budget_seconds", 150)
C.JOBS_POOL_WORKERS = CFG.get("jobs_pool_workers", 8)

GH_TIMEOUT_S = CFG.get("gh_timeout_seconds", 20)


def _gh_json(args):
    return G.gh_json(args, timeout=GH_TIMEOUT_S)


LOCAL_PROBE_CFG = CFG.get("local_probe")
LOCAL_PROBE_PATTERN = re.compile(LOCAL_PROBE_CFG["pattern"]) if LOCAL_PROBE_CFG else None

# restart_command is entirely optional and platform-specific (see README). Absent, the
# restart feature is off: the UI hides the buttons (snapshot carries restart_enabled),
# and POST /api/restart refuses with 400. CI_DASH_RESTART_CMD is a test-only override.
_restart_cmd_env = os.environ.get("CI_DASH_RESTART_CMD")
if _restart_cmd_env is not None:
    RESTART_CMD = shlex.split(_restart_cmd_env)
else:
    RESTART_CMD = list(CFG["restart_command"]) if CFG.get("restart_command") else None
RESTART_ENABLED = RESTART_CMD is not None
RESTART_TIMEOUT_S = 15

LOG_TAIL_LINES = 60

_log_path_env = os.environ.get("CI_DASH_LOG_PATH")
_log_path_cfg = _log_path_env or CFG.get("actions_log_path") or "actions.log"
ACTIONS_LOG = pathlib.Path(_log_path_cfg)
if not ACTIONS_LOG.is_absolute():
    ACTIONS_LOG = BASE_DIR / ACTIONS_LOG
ACTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)


def log_action(event, **fields):
    line = f"{datetime.now(timezone.utc).isoformat()} {event} {json.dumps(fields, default=str)}\n"
    with ACTIONS_LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    print(line.rstrip(), file=sys.stderr)


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot = {"fetched_at": None, "pass_seconds": None, "rate_limit": {}, "banner": "first pass pending",
                         "partial": False, "owner": C.OWNER, "repos": {r: {} for r in C.REPOS}}
        self.local_probe = {"checked_at": None, "error": "not run yet", "dirs": None}
        self.restart = R.RestartState()
        self.cache = C.RunCache()
        self.wake = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=C.JOBS_POOL_WORKERS)
        self.log_cache = {}
        self.next_pass_at = time.time()


STATE = State()


def local_probe_once():
    if NO_LOCAL_PROBE:
        return {"checked_at": None, "error": "probe disabled (CI_DASH_NO_LOCAL_PROBE)", "dirs": None}
    if not LOCAL_PROBE_CFG:
        # No local_probe configured: never runs a command, always reports unknown
        # (dirs=None), never a fabricated "dead" runner.
        return {"checked_at": None, "error": None, "dirs": None}
    try:
        result = subprocess.run(LOCAL_PROBE_CFG["command"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=LOCAL_PROBE_TIMEOUT_S)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr.strip() or "local probe command failed")
        dirs = C.parse_local_probe(result.stdout, LOCAL_PROBE_PATTERN)
        return {"checked_at": datetime.now(timezone.utc).isoformat(), "error": None, "dirs": sorted(dirs)}
    except subprocess.TimeoutExpired:
        return {"checked_at": None, "error": f"local probe timed out after {LOCAL_PROBE_TIMEOUT_S}s", "dirs": None}
    except Exception as exc:
        return {"checked_at": None, "error": str(exc), "dirs": None}


def local_probe_loop():
    # Own thread, own timeout. A wedged probe command must be the only thing that can
    # stall when that happens, never the collector or the HTTP layer.
    while True:
        probe = local_probe_once()
        with STATE.lock:
            if probe["error"] and STATE.local_probe.get("checked_at"):
                # Keep the last good checked_at so the UI can say "stale since HH:MM"
                # instead of losing the timestamp; still surface the real error string,
                # and never claim runners are locally dead just because the probe
                # itself could not run.
                probe["checked_at"] = STATE.local_probe["checked_at"]
                probe["dirs"] = None
            STATE.local_probe = probe
        time.sleep(LOCAL_PROBE_INTERVAL_S)


def _fire_restart():
    try:
        subprocess.run(RESTART_CMD, capture_output=True, text=True, timeout=RESTART_TIMEOUT_S)
        log_action("restart_fired", cmd=RESTART_CMD)
        return None
    except subprocess.TimeoutExpired:
        log_action("restart_fire_timeout", cmd=RESTART_CMD)
        return "restart command did not respond"
    except Exception as exc:
        log_action("restart_fire_error", error=str(exc))
        return str(exc)


def _rerun(repo, run_id):
    try:
        _gh_json(["api", "-X", "POST", f"repos/{C.OWNER}/{repo}/actions/runs/{run_id}/rerun-failed-jobs"])
        log_action("rerun_requested", repo=repo, run_id=run_id)
        return None
    except Exception as exc:
        log_action("rerun_failed", repo=repo, run_id=run_id, error=str(exc))
        return str(exc)


def drive_restart(snapshot, now):
    """Advance the restart machine against a fresh snapshot and execute its actions."""
    with STATE.lock:
        state = STATE.restart
    new_state, actions = R.step(state, snapshot, now)
    for action in actions:
        if action["type"] == "fire":
            err = _fire_restart()
            if err:
                new_state.state, new_state.message = "failed", err
        elif action["type"] == "rerun":
            err = _rerun(action["repo"], action["run_id"])
            for item in new_state.interrupted:
                if item["repo"] == action["repo"] and item["run_id"] == action["run_id"]:
                    item["rerun"] = "requested" if err is None else f"failed: {err}"
    if new_state.state != state.state:
        log_action("restart_state", state=new_state.state, message=new_state.message)
    with STATE.lock:
        STATE.restart = new_state


def run_pass():
    now = time.time()
    with STATE.lock:
        local_probe = dict(STATE.local_probe)
        cache = STATE.cache
        pool = STATE.pool
    try:
        snap = C.build_snapshot(_gh_json, G.gh_paginate, cache, local_probe, now, pool)
    except Exception as exc:
        with STATE.lock:
            STATE.snapshot["banner"] = f"last pass failed: {exc}"
        print(f"pass failed: {exc}", file=sys.stderr)
        return
    drive_restart(snap, now)
    with STATE.lock:
        snap["restart"] = R.to_dict(STATE.restart)
        snap["restart_enabled"] = RESTART_ENABLED
        snap["next_pass_at"] = STATE.next_pass_at
        STATE.snapshot = snap


def collector_loop():
    # Never runs on the HTTP handler thread. /api/status only ever reads the snapshot
    # this loop last published; a slow pass can never block a status request.
    while True:
        run_pass()
        with STATE.lock:
            busy_restart = STATE.restart.state in ("restarting", "verifying")
            STATE.next_pass_at = time.time() + (30 if busy_restart else INTERVAL_S)
            wait = STATE.next_pass_at - time.time()
        STATE.wake.wait(timeout=max(wait, 1))
        STATE.wake.clear()


def fetch_log_tail(repo, job_id):
    key = (repo, job_id)
    if key in STATE.log_cache:
        return STATE.log_cache[key]
    # Same encoding fix as gh_client._run: job logs are real-world text, not
    # guaranteed ASCII/legacy-codepage-safe.
    result = subprocess.run([G.resolve_gh(), "api", f"repos/{C.OWNER}/{repo}/actions/jobs/{job_id}/logs"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=GH_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh failed")
    tail = "\n".join(result.stdout.splitlines()[-LOG_TAIL_LINES:])
    STATE.log_cache[key] = tail
    return tail


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = (BASE_DIR / "dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            # Instant: only ever reads the last completed snapshot under the lock.
            # Never calls build_snapshot. partial/partial_message pass through
            # unchanged, straight from collector.build_snapshot's own output.
            with STATE.lock:
                snap = dict(STATE.snapshot)
                snap["restart"] = R.to_dict(STATE.restart)
                snap["restart_enabled"] = RESTART_ENABLED
                snap["local_probe"] = dict(STATE.local_probe)
            snap["served_at"] = time.time()
            self._json(200, snap)
        elif self.path == "/api/restart":
            with STATE.lock:
                self._json(200, R.to_dict(STATE.restart))
        elif self.path.startswith("/api/log?"):
            q = parse_qs(urlparse(self.path).query)
            repo, job_id = q.get("repo", [""])[0], q.get("job_id", [""])[0]
            if repo not in C.REPOS or not job_id.isdigit():
                return self._json(400, {"error": "bad repo or job_id"})
            try:
                self._json(200, {"tail": fetch_log_tail(repo, job_id)})
            except Exception as exc:
                self._json(502, {"error": str(exc)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/refresh":
            STATE.wake.set()
            return self._json(202, {"ok": True})
        if self.path == "/api/restart":
            if not RESTART_ENABLED:
                return self._json(400, {"error": "restart is not configured (set restart_command in config.json)"})
            body = self._body()
            mode = body.get("mode")
            with STATE.lock:
                snap = STATE.snapshot
                targets = [(repo, r["name"]) for repo in C.REPOS for r in snap["repos"].get(repo, {}).get("runners", [])]
                prev_state = STATE.restart
                # Pass mode straight through to restart.request()/restart.force() --
                # those are the single source of truth for which modes are legal
                # (restart.request accepts exactly "idle" and "now"). No separate
                # hardcoded allowlist here: that would be a second copy of the
                # legality rule that could silently drift from the state machine's.
                if mode == "force":
                    new_state = R.force(prev_state, snap, time.time())
                else:
                    new_state = R.request(prev_state, mode, targets, snap, time.time())
                # A rejected mode comes back with state.state UNCHANGED and a message
                # naming the rejected value -- that is how restart.request signals
                # "refused", not an exception. Detect it exactly that way.
                rejected = (
                    new_state.state == prev_state.state
                    and new_state.message
                    and "rejected" in new_state.message
                )
                if rejected:
                    return self._json(400, {"error": new_state.message})
                STATE.restart = new_state
                state = new_state
            log_action("restart_requested", mode=mode, targets=targets)
            STATE.wake.set()
            return self._json(202, R.to_dict(state))
        self._json(404, {"error": "not found"})

    def log_message(self, format, *args):
        pass


def main():
    auth = subprocess.run([G.resolve_gh(), "auth", "status"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if auth.returncode != 0:
        print("gh is not authenticated: run `gh auth login` first.", file=sys.stderr)
        sys.exit(1)
    threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=local_probe_loop, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"CI dashboard at http://localhost:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
