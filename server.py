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
  - gh_status_loop() runs on its own daemon thread, polling githubstatus.com (not the
    GitHub API, so it spends no API quota). A failed fetch keeps the last good reading,
    marked stale; it never reports "operational" for something it could not read.

The collector loop runs a full pass on its timer (or on "Refresh now"), and a partial
pass over just the repos a signed webhook named (see POST /api/webhook). Webhooks are
only ever a hint to refresh early; the page never shows webhook payload data as state.
"""
import hashlib
import hmac
import http.server
import math
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
import gh_status as S
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
MIN_REFRESH_S = int(os.environ.get("CI_DASH_MIN_REFRESH", CFG["min_refresh_seconds"]))

GH_STATUS_URL = os.environ.get("CI_DASH_GH_STATUS_URL", S.STATUS_URL)
GH_STATUS_INTERVAL_S = CFG["status_poll_seconds"]
GH_STATUS_TIMEOUT_S = 10
GH_STATUS_ENABLED = os.environ.get("CI_DASH_NO_GH_STATUS") != "1" and GH_STATUS_INTERVAL_S > 0
GH_STATUS_COMPONENTS = CFG.get("gh_status_components") or S.DEFAULT_COMPONENTS

# Webhooks are off unless a secret is configured (CI_DASH_WEBHOOK_SECRET is a test-only
# override). Unsigned or wrongly signed deliveries are always rejected.
WEBHOOK_SECRET = os.environ.get("CI_DASH_WEBHOOK_SECRET") or CFG.get("webhook_secret")
WEBHOOK_SWEEP_S = CFG["webhook_sweep_seconds"]
WEBHOOK_MAX_BYTES = 10 * 1024 * 1024
WEBHOOK_COALESCE_S = float(os.environ.get("CI_DASH_WEBHOOK_COALESCE", "5"))
WEBHOOK_EVENTS = {"workflow_run", "workflow_job", "pull_request", "check_suite", "check_run"}
# A run created this long after the last webhook, with no webhook since, means
# deliveries are not arriving (allows for clock skew and delivery latency).
WEBHOOK_MISS_GRACE_S = 120


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
LOG_CACHE_LIMIT = 200

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
        self.sha_cache = C.ShaRunsCache(ttl=CFG["sha_cache_ttl_seconds"])
        self.wake = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=C.JOBS_POOL_WORKERS)
        # Only completed jobs' log tails are cached (an in-progress job's log is still
        # growing), bounded like the jobs cache.
        self.log_cache = C.RunCache(limit=LOG_CACHE_LIMIT)
        self.next_pass_at = time.time()
        self.full_requested = False
        self.dirty_repos = set()
        self.pass_running = False
        self.last_pass_started_at = None
        self.last_pass_finished_at = None
        self.last_pass_ok_at = None
        self.last_pass_error = None
        self.backoff_reason = None
        self.collector_thread = None
        self.gh_health = {"enabled": GH_STATUS_ENABLED, "checked_at": None, "stale": True,
                          "error": "not run yet" if GH_STATUS_ENABLED else "disabled",
                          "indicator": None, "description": None, "components": None,
                          "incidents": [], "page_url": S.PAGE_URL}
        self.webhooks = {"enabled": bool(WEBHOOK_SECRET), "last_at": None, "last_event": None,
                         "received": 0, "ignored": 0, "rejected": 0, "missed": None}


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


def gh_status_once(prev):
    try:
        parsed = S.parse_summary(S.fetch_summary(GH_STATUS_URL, timeout=GH_STATUS_TIMEOUT_S),
                                 GH_STATUS_COMPONENTS)
        return {**parsed, "enabled": True, "checked_at": datetime.now(timezone.utc).isoformat(),
                "stale": False, "error": None}
    except Exception as exc:
        # Keep the last good reading (and its checked_at) so the UI can say "stale since
        # HH:MM", but mark it stale: stale data is never used to explain errors or to
        # back off, and is never shown as an all-clear.
        out = dict(prev)
        out.update(enabled=True, stale=True, error=str(exc) or type(exc).__name__)
        return out


def gh_status_loop():
    if not GH_STATUS_ENABLED:
        return
    while True:
        with STATE.lock:
            prev = dict(STATE.gh_health)
        health = gh_status_once(prev)
        with STATE.lock:
            STATE.gh_health = health
        time.sleep(GH_STATUS_INTERVAL_S)


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


def _merge_partial(prev, part):
    """Fold a partial (subset-of-repos) snapshot into the last full one. Top-level
    fetched_at/api_calls/partial keep describing the last FULL pass; each repo section
    carries its own fetched_at."""
    merged = dict(prev)
    merged["repos"] = {**(prev.get("repos") or {}), **part["repos"]}
    merged["rate_limit"] = part["rate_limit"]
    merged["banner"] = part["banner"]
    merged["partial_pass_at"] = part["fetched_at"]
    merged["partial_pass_api_calls"] = part["api_calls"]
    kinds = set()
    for section in merged["repos"].values():
        kinds.update((section or {}).get("error_kinds") or [])
    merged["error_kinds"] = sorted(kinds)
    return merged


def _check_webhooks_missed(snap, now):
    """Evidence-based webhook health: a run GitHub created well after the last delivery,
    with no delivery since, means webhooks are not reaching us."""
    with STATE.lock:
        last_at = STATE.webhooks["last_at"]
    if not WEBHOOK_SECRET or last_at is None:
        return None
    for repo, section in (snap.get("repos") or {}).items():
        try:
            created = C.parse_ts((section or {}).get("latest_run_created_at"))
        except ValueError:
            continue  # unparseable timestamp: no evidence either way
        if created and created > last_at + WEBHOOK_MISS_GRACE_S and now - created > 60:
            return (f"{repo}: a run created at {section['latest_run_created_at']} produced no "
                    f"webhook; falling back to the normal poll interval")
    return None


def run_pass(repos=None):
    """One collector pass (all repos, or just `repos`). Returns the published snapshot,
    or None when the pass failed (the previous snapshot stays up, with a banner)."""
    now = time.time()
    with STATE.lock:
        local_probe = dict(STATE.local_probe)
        cache, pool, sha_cache = STATE.cache, STATE.pool, STATE.sha_cache
        STATE.pass_running = True
        STATE.last_pass_started_at = now
    try:
        snap = C.build_snapshot(_gh_json, G.gh_paginate, cache, local_probe, now, pool,
                                repos=repos, sha_cache=sha_cache)
        if repos is not None:
            with STATE.lock:
                prev = STATE.snapshot
            snap = _merge_partial(prev, snap)
    except Exception as exc:
        with STATE.lock:
            STATE.snapshot = {**STATE.snapshot, "banner": f"last pass failed: {exc}"}
            STATE.last_pass_error = str(exc)
            STATE.pass_running = False
            STATE.last_pass_finished_at = time.time()
        print(f"pass failed: {exc}", file=sys.stderr)
        return None
    try:
        drive_restart(snap, now)
    except Exception as exc:
        # The restart machine failing must not throw away a good snapshot.
        log_action("restart_drive_error", error=str(exc))
        snap["banner"] = f"restart handling failed: {exc}"
    missed = _check_webhooks_missed(snap, now) if repos is None else None
    with STATE.lock:
        snap["restart"] = R.to_dict(STATE.restart)
        snap["restart_enabled"] = RESTART_ENABLED
        STATE.snapshot = snap
        STATE.pass_running = False
        STATE.last_pass_finished_at = time.time()
        STATE.last_pass_ok_at = now
        STATE.last_pass_error = None
        if repos is None:
            STATE.webhooks["missed"] = missed
    return snap


def _webhooks_trusted():
    w = STATE.webhooks
    return bool(WEBHOOK_SECRET) and w["last_at"] is not None and not w["missed"]


def _schedule_next(snap):
    """Pick the next full-pass time: the base interval (longer while webhooks are
    demonstrably arriving), stretched by collector.next_interval's backoff rules."""
    now = time.time()
    with STATE.lock:
        base = max(INTERVAL_S, WEBHOOK_SWEEP_S) if _webhooks_trusted() else INTERVAL_S
        outage = S.major_outage(STATE.gh_health)
        busy_restart = STATE.restart.state in ("restarting", "verifying")
    if snap is None:
        interval, reason = base, None
    else:
        interval, reason = C.next_interval(base, snap, now, gh_outage=outage)
    if busy_restart and reason is None:
        interval = 30
    with STATE.lock:
        STATE.next_pass_at = now + interval
        STATE.backoff_reason = reason


def _collector_tick():
    with STATE.lock:
        full = STATE.full_requested or time.time() >= STATE.next_pass_at
        has_dirty = bool(STATE.dirty_repos)
    if not full and has_dirty:
        # Webhooks arrive in bursts (one push -> several events): let them coalesce,
        # and never run passes closer together than min_refresh_seconds.
        with STATE.lock:
            since = time.time() - (STATE.last_pass_finished_at or 0)
        time.sleep(max(WEBHOOK_COALESCE_S, MIN_REFRESH_S - since))
    with STATE.lock:
        full = full or STATE.full_requested or time.time() >= STATE.next_pass_at
        dirty = sorted(STATE.dirty_repos)
        STATE.full_requested = False
        STATE.dirty_repos = set()
    if full:
        _schedule_next(run_pass())
    elif dirty:
        run_pass(repos=dirty)


def collector_loop():
    # Never runs on the HTTP handler thread. /api/status only ever reads the snapshot
    # this loop last published; a slow pass can never block a status request. Nothing
    # may escape this loop: an uncaught exception would end the thread and freeze the
    # dashboard on its last snapshot with no sign anything was wrong.
    while True:
        try:
            _collector_tick()
        except Exception as exc:
            print(f"collector error: {exc}", file=sys.stderr)
            with STATE.lock:
                STATE.last_pass_error = f"collector error: {exc}"
                STATE.pass_running = False
                STATE.next_pass_at = time.time() + INTERVAL_S
        with STATE.lock:
            wait = STATE.next_pass_at - time.time()
        STATE.wake.wait(timeout=max(wait, 1))
        STATE.wake.clear()


def collector_health(now):
    with STATE.lock:
        t = STATE.collector_thread
        alive = t is not None and t.is_alive()
        # The next pass should have finished by next_pass_at + budget + slack; past
        # that, the data on screen is older than it claims to be.
        stale = not alive or now > STATE.next_pass_at + C.PASS_BUDGET_S + 120
        return {
            "alive": alive,
            "pass_running": STATE.pass_running,
            "last_ok_at": STATE.last_pass_ok_at,
            "last_error": STATE.last_pass_error,
            "backoff_reason": STATE.backoff_reason,
            "stale": stale,
            "min_refresh_seconds": MIN_REFRESH_S,
        }


def _job_status(snap, repo, job_id):
    section = (snap.get("repos") or {}).get(repo) or {}
    runs = [r for p in section.get("prs") or [] for r in p.get("runs") or []]
    runs += list(section.get("deploys") or [])
    for run in runs:
        for job in run.get("jobs") or []:
            if str(job.get("job_id")) == str(job_id):
                return job.get("status")
    return None


def fetch_log_tail(repo, job_id):
    key = (repo, job_id)
    with STATE.lock:
        cached = STATE.log_cache.get(key)
        status = _job_status(STATE.snapshot, repo, job_id)
    if cached is not None:
        return cached
    out = G.gh_text(["api", f"repos/{C.OWNER}/{repo}/actions/jobs/{job_id}/logs"], timeout=GH_TIMEOUT_S)
    tail = "\n".join(out.splitlines()[-LOG_TAIL_LINES:])
    if status == "completed":
        with STATE.lock:
            STATE.log_cache.put(key, tail)
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
            now = time.time()
            with STATE.lock:
                snap = dict(STATE.snapshot)
                snap["restart"] = R.to_dict(STATE.restart)
                snap["restart_enabled"] = RESTART_ENABLED
                snap["local_probe"] = dict(STATE.local_probe)
                snap["next_pass_at"] = STATE.next_pass_at
                health = dict(STATE.gh_health)
                snap["webhooks"] = dict(STATE.webhooks)
            health["note"] = S.explain_errors(snap, health)
            snap["gh_health"] = health
            snap["collector"] = collector_health(now)
            snap["served_at"] = now
            self._json(200, snap)
        elif self.path == "/api/health":
            col = collector_health(time.time())
            ok = col["alive"] and not col["stale"] and col["last_error"] is None
            with STATE.lock:
                indicator = STATE.gh_health.get("indicator")
            self._json(200 if ok else 503, {"ok": ok, "collector": col, "github_indicator": indicator})
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
            with STATE.lock:
                if STATE.pass_running:
                    return self._json(202, {"ok": True, "note": "a pass is already running"})
                since = time.time() - (STATE.last_pass_finished_at or 0)
                if STATE.last_pass_finished_at is not None and since < MIN_REFRESH_S:
                    wait = math.ceil(MIN_REFRESH_S - since)
                    return self._json(429, {"error": f"last pass finished {int(since)}s ago; "
                                                     f"try again in {wait}s", "retry_after": wait})
                STATE.full_requested = True
            STATE.wake.set()
            return self._json(202, {"ok": True})
        if self.path == "/api/webhook":
            return self._webhook()
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
                STATE.full_requested = True
                state = new_state
            log_action("restart_requested", mode=mode, targets=targets)
            STATE.wake.set()
            return self._json(202, R.to_dict(state))
        self._json(404, {"error": "not found"})

    def _webhook(self):
        if not WEBHOOK_SECRET:
            return self._json(404, {"error": "webhooks are not configured (set webhook_secret in config.json)"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0:
            return self._json(400, {"error": "bad Content-Length"})
        if n > WEBHOOK_MAX_BYTES:
            return self._json(413, {"error": "payload too large"})
        body = self.rfile.read(n)
        expected = b"sha256=" + hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest().encode()
        sig = (self.headers.get("X-Hub-Signature-256") or "").encode("utf-8", "replace")
        if not hmac.compare_digest(sig, expected):
            with STATE.lock:
                STATE.webhooks["rejected"] += 1
            return self._json(401, {"error": "missing or invalid X-Hub-Signature-256"})
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return self._json(400, {"error": "body is not JSON"})
        event = self.headers.get("X-GitHub-Event") or ""
        repo_obj = (payload.get("repository") or {}) if isinstance(payload, dict) else {}
        repo = repo_obj.get("name")
        owner = (repo_obj.get("owner") or {}).get("login") or ""
        relevant = (event in WEBHOOK_EVENTS and repo in C.REPOS
                    and owner.lower() == (C.OWNER or "").lower())
        with STATE.lock:
            STATE.webhooks["last_at"] = time.time()
            STATE.webhooks["last_event"] = event
            STATE.webhooks["received"] += 1
            if relevant:
                STATE.dirty_repos.add(repo)
            elif event != "ping":
                STATE.webhooks["ignored"] += 1
        if relevant:
            STATE.wake.set()
            return self._json(202, {"ok": True, "refresh": repo})
        return self._json(202, {"ok": True, "ignored": event != "ping"})

    def log_message(self, format, *args):
        pass


def main():
    auth = subprocess.run([G.resolve_gh(), "auth", "status"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if auth.returncode != 0:
        print("gh is not authenticated: run `gh auth login` first.", file=sys.stderr)
        sys.exit(1)
    STATE.collector_thread = threading.Thread(target=collector_loop, daemon=True)
    STATE.collector_thread.start()
    threading.Thread(target=local_probe_loop, daemon=True).start()
    threading.Thread(target=gh_status_loop, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"CI dashboard at http://localhost:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
