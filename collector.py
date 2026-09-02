"""Pure transforms from raw gh JSON to the dashboard snapshot. No I/O here.

OWNER, REPOS, DEPLOY_WORKFLOW_PATHS, RUNNER_LOCAL_DIRS, PASS_BUDGET_S and
JOBS_POOL_WORKERS are module-level so the rest of this file can read them as plain
names, but they hold no built-in values -- server.py sets them from config.json (see
config.py) right after import, before the first collector pass runs. Tests set them
directly on this module for the same reason.
"""
import re
from datetime import datetime, timezone

OWNER = None
REPOS = []
FAILURE_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "action_required"})
ROLLUP_ORDER = ["failure", "unknown", "running", "queued", "skipped", "success", "no_ci"]

# First match wins. "setup" is hidden from chips. Order matters: typecheck before build,
# and sentinels before tests (a guard/sweep step whose name happens to contain the
# literal word "tests" -- e.g. a step that audits a repo's own tests/ directory -- must
# read as a sentinels finding, not get lost under the tests chip).
CATEGORY_PATTERNS = [
    (
        "setup",
        re.compile(
            r"checkout|set up job|actions/setup|^install\b|npm ci"
            r"|actions/cache|restore cache|save cache|^post "
            r"|complete job|assume oidc|fetch .*credentials|report .*outcome"
            r"|^locate |^print |playwright browsers",
            re.I,
        ),
    ),
    ("lint", re.compile(r"lint|eslint|stylelint|ruff|flake8|prettier", re.I)),
    ("typecheck", re.compile(r"typecheck|\btsc\b|mypy", re.I)),
    ("perf", re.compile(r"lighthouse|bundle size", re.I)),
    ("visual", re.compile(r"visual regression|playwright visual|\bvisual\b", re.I)),
    (
        # Ordered ahead of "tests" deliberately: a structural guard/sweep step whose
        # name happens to contain the literal path fragment "tests/" (e.g. a step that
        # audits the repo's own test suite for coverage or architecture) must surface
        # under sentinels, not tests -- sending an operator to the tests chip for a
        # tripped structural guard sends them to the wrong place.
        "sentinels",
        re.compile(
            r"sentinel|guard|drift|integrity|tripwire|census|\bgate"
            r"|npm audit|verify_modularity|coverage sweep",
            re.I,
        ),
    ),
    ("tests", re.compile(r"pytest|vitest|\btests?\b", re.I)),
    ("deploy", re.compile(r"deploy|promote|alias|rollback", re.I)),
    ("build", re.compile(r"\bbuild\b", re.I)),
]
VISIBLE_CATEGORIES = ["lint", "typecheck", "tests", "perf", "visual", "sentinels", "build", "deploy"]

# Step names that are legitimately uncategorizable (deliberately left in "other"), each
# with why. Adding a name here should come with a one-line justification.
ALLOWED_OTHER = {
    # Generic, unqualified post-step name shared by many kinds of jobs (test jobs,
    # sentinel jobs, etc. all use "Verify results" as a closing step). There is no
    # safe, narrow keyword to key off of that would not risk swallowing unrelated
    # future step names into whatever category we picked; "other" is the honest
    # answer here.
    "Verify results": "generic unqualified post-step name, no safe category-specific keyword",
}


def categorize_step(name):
    for category, pattern in CATEGORY_PATTERNS:
        if pattern.search(name or ""):
            return category
    return "other"


def parse_ts(s):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def normalize_job(raw, now):
    created = parse_ts(raw.get("created_at"))
    started = parse_ts(raw.get("started_at"))
    completed = parse_ts(raw.get("completed_at"))
    status = raw.get("status")
    if status in ("queued", "waiting", "pending"):
        queued_s = (now - created) if created else None
    else:
        queued_s = (started - created) if (started and created) else None
    if started and completed:
        duration_s = completed - started
    elif started and status == "in_progress":
        duration_s = now - started
    else:
        duration_s = None
    return {
        "job_id": raw["id"],
        "run_id": raw.get("run_id"),
        "name": raw.get("name"),
        "status": status,
        "conclusion": raw.get("conclusion"),
        "runner_name": raw.get("runner_name"),
        "started_at": raw.get("started_at"),
        "completed_at": raw.get("completed_at"),
        "created_at": raw.get("created_at"),
        "queued_s": None if queued_s is None else round(queued_s),
        "duration_s": None if duration_s is None else round(duration_s),
        "url": raw.get("html_url"),
        "steps": [
            {
                "number": s.get("number"),
                "name": s.get("name"),
                "status": s.get("status"),
                "conclusion": s.get("conclusion"),
                "category": categorize_step(s.get("name")),
            }
            for s in raw.get("steps") or []
        ],
    }


def normalize_run(raw, jobs):
    return {
        "run_id": raw["id"],
        "workflow": raw.get("name"),
        "path": raw.get("path"),
        "event": raw.get("event"),
        "head_branch": raw.get("head_branch"),
        "head_sha": raw.get("head_sha"),
        "status": raw.get("status"),
        "conclusion": raw.get("conclusion"),
        "url": raw.get("html_url"),
        "created_at": raw.get("created_at"),
        "run_attempt": raw.get("run_attempt"),
        "jobs": jobs,
    }


def _state(status, conclusion):
    if status == "completed":
        if conclusion in FAILURE_CONCLUSIONS:
            return "failure"
        if conclusion == "skipped":
            return "skipped"
        return "success"
    if status == "in_progress":
        return "running"
    return "queued"  # queued, waiting, pending, requested


def job_state(job):
    return _state(job.get("status"), job.get("conclusion"))


def rollup(states):
    if not states:
        return "no_ci"
    present = set(states)
    if "failure" in present:
        return "failure"
    # "unknown" (a run whose jobs we could not fetch -- gh error or pass-budget cutoff)
    # outranks every optimistic state. A definite failure is still definite and wins
    # over an unknown, but a PR must never report running/queued/success/no_ci while
    # part of its real state is unretrieved -- that would read as "this PR is fine" when
    # the honest answer is "we do not know".
    if "unknown" in present:
        return "unknown"
    if "running" in present:
        return "running"
    if "queued" in present:
        return "queued"
    if present == {"skipped"}:
        return "skipped"
    return "success"


def counts(jobs):
    out = {"executed": 0, "running": 0, "queued": 0, "failed": 0, "skipped": 0}
    for job in jobs:
        s = job_state(job)
        if s == "success":
            out["executed"] += 1
        elif s == "failure":
            out["failed"] += 1
            out["executed"] += 1
        elif s == "running":
            out["running"] += 1
        elif s == "queued":
            out["queued"] += 1
        elif s == "skipped":
            out["skipped"] += 1
    return out


def chips(jobs):
    by_cat = {}
    for job in jobs:
        steps = job.get("steps") or []
        if not steps:
            # GitHub does not populate a job's `steps` array until the job starts, so
            # every queued job has steps=[]. Falling through silently here means chips
            # is {} for the common case (a PR with anything queued) -- categorize the
            # JOB NAME itself with the same table and let that stand in for the job's
            # one category contribution, instead of contributing nothing.
            cat = categorize_step(job.get("name"))
            if cat in VISIBLE_CATEGORIES:
                by_cat.setdefault(cat, []).append(job_state(job))
            continue
        for step in steps:
            cat = step.get("category")
            if cat not in VISIBLE_CATEGORIES:
                continue
            # A step that has not started inherits the job's state (queued/running).
            if step.get("status") == "completed":
                st = _state("completed", step.get("conclusion"))
            else:
                st = job_state(job)
            by_cat.setdefault(cat, []).append(st)
    return {cat: rollup(states) for cat, states in by_cat.items()}


def build_pr(raw_pr, runs):
    jobs = [j for r in runs for j in r["jobs"]]
    states = [job_state(j) for j in jobs]
    # A run whose jobs fetch failed (or was cut by the pass budget) is marked
    # `jobs_unknown` by collect_repo/_fetch_jobs_for. Its jobs list is empty either
    # way, so without this the run would silently read the same as a run with
    # genuinely zero jobs and could flip the PR's rollup to no_ci/success. Folding an
    # explicit "unknown" state into the rollup input keeps the two distinguishable.
    for run in runs:
        if run.get("jobs_unknown"):
            states.append("unknown")
    return {
        "number": raw_pr["number"],
        "title": raw_pr.get("title"),
        "branch": raw_pr["head"]["ref"],
        "sha": raw_pr["head"]["sha"],
        "url": raw_pr.get("html_url"),
        "updated_at": raw_pr.get("updated_at"),
        "rollup": rollup(states),
        "counts": counts(jobs),
        "chips": chips(jobs),
        "runs": runs,
    }


# Populated from config.json's `runner_local_dirs` by server.py, as
# {(repo, runner_name): local_dir_name}. Empty by default -- with no entries,
# attach_runner_jobs reports every runner's local_alive as None (unknown), never dead.
RUNNER_LOCAL_DIRS = {}

# Populated from config.json's `deploy_workflows` by server.py, as {repo: {path, ...}}.
DEPLOY_WORKFLOW_PATHS = {}


def parse_local_probe(text, pattern):
    """Extract the set of runner directory names from a local-process listing.

    `pattern` is a compiled regex with exactly one capture group naming the runner
    directory (see config.json's `local_probe.pattern`)."""
    return {m.group(1) for m in pattern.finditer(text or "")}


def build_queue(runs, pr_by_sha):
    items = []
    for run in runs:
        for job in run["jobs"]:
            if job_state(job) != "queued":
                continue
            items.append({
                "job_id": job["job_id"],
                "name": job["name"],
                "workflow": run.get("workflow"),
                "run_url": run.get("url"),
                "url": job.get("url"),
                "pr_number": pr_by_sha.get(run.get("head_sha")),
                "queued_at": job.get("created_at"),
                "waited_s": job.get("queued_s"),
            })
    # Unknown wait (waited_s is None, meaning normalize_job could not compute queued_s
    # because created_at was missing) is a stronger anomaly signal than any known wait
    # and sorts first, ahead of the oldest known waits. Known waits then sort oldest first.
    items.sort(key=lambda x: (x["waited_s"] is not None, -(x["waited_s"] or 0)))
    return items


def attach_runner_jobs(runners, runs, repo, local_dirs):
    running_by_runner = {}
    for run in runs:
        for job in run["jobs"]:
            if job.get("status") == "in_progress" and job.get("runner_name"):
                running_by_runner[job["runner_name"]] = {**job, "workflow": run.get("workflow"), "run_url": run.get("url")}
    out = []
    for r in runners:
        local_dir = RUNNER_LOCAL_DIRS.get((repo, r["name"]))
        alive = None if (local_dirs is None or local_dir is None) else (local_dir in local_dirs)
        out.append({
            "name": r["name"], "status": r.get("status"), "busy": bool(r.get("busy")),
            "local_alive": alive, "current_job": running_by_runner.get(r["name"]),
        })
    return out


def select_deploys(runs, repo, limit=10):
    paths = DEPLOY_WORKFLOW_PATHS.get(repo, set())
    chosen = [r for r in runs if r.get("path") in paths and r.get("head_branch") == "main"
              and r.get("event") != "pull_request"]
    chosen.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return chosen[:limit]


import time as _time
from collections import OrderedDict
from concurrent.futures import as_completed


class RunCache:
    """LRU cache of jobs-by-run-id for completed runs, bounded to `limit` entries."""

    def __init__(self, limit=500):
        self.limit = limit
        self._d = OrderedDict()

    def get(self, run_id):
        if run_id in self._d:
            self._d.move_to_end(run_id)
            return self._d[run_id]
        return None

    def put(self, run_id, jobs):
        self._d[run_id] = jobs
        self._d.move_to_end(run_id)
        while len(self._d) > self.limit:
            self._d.popitem(last=False)


def _repo_path(repo, suffix):
    return f"repos/{OWNER}/{repo}/{suffix}"


PASS_BUDGET_S = 150
JOBS_POOL_WORKERS = 8


def _fetch_runs(repo, fetch, shas, errors):
    """A recency-window listing (`actions/runs?per_page=50`) can miss an older PR's run
    entirely if that PR's only CI run predates the window, so it never gets fetched and
    `no_ci` fabricates "this PR has no CI" out of "we did not look". Widening the window
    only moves the same bug to an even older PR, so instead every open PR's head sha is
    queried directly (`actions/runs?head_sha=<sha>`), in addition to the recency/queued/
    in_progress listings the queue and fleet views still need. Everything is unioned by
    run id.

    Each suffix (including each per-sha query) is fetched in its own try/except: one
    PR's sha query failing must not blank every other PR's runs, the queued listing, or
    the in-progress listing. Failures are recorded per suffix under `errors["runs"]`
    (a list, not a single string, so distinct failures are not dropped) rather than
    aborting the whole fetch."""
    seen = OrderedDict()
    suffixes = [
        "actions/runs?per_page=50",
        "actions/runs?status=queued&per_page=100",
        "actions/runs?status=in_progress&per_page=100",
    ]
    suffixes += [f"actions/runs?head_sha={sha}&per_page=100" for sha in shas]
    for suffix in suffixes:
        try:
            data = fetch(["api", _repo_path(repo, suffix)]) or {}
        except Exception as exc:
            lst = errors.setdefault("runs", [])
            msg = str(exc)
            if msg not in lst:
                lst.append(msg)
            continue
        for raw in data.get("workflow_runs", []):
            seen.setdefault(raw["id"], raw)
    return list(seen.values())


def _fetch_jobs_for(repo, raw_runs, paginate, cache, now, pool, errors, deadline, time_fn, budget_skipped):
    """Returns (jobs_by_run, unknown_run_ids). A run id lands in `unknown_run_ids` when
    its jobs could not be retrieved -- a `gh` failure or a pass-budget cutoff -- so the
    caller can mark it `jobs_unknown` and the run reads as genuinely unknown rather than
    silently as "zero jobs" (which `rollup([])` would read as the same green/no_ci as a
    run that really has none)."""
    jobs_by_run = {}
    unknown_ids = set()
    pending = []
    for raw in raw_runs:
        cached = cache.get(raw["id"])
        if raw.get("status") == "completed" and cached is not None:
            jobs_by_run[raw["id"]] = cached
        else:
            pending.append(raw)

    futures = {}
    for raw in pending:
        if deadline is not None and time_fn() >= deadline:
            # Pass-budget exceeded -- stop issuing new jobs fetches. This run (and
            # everything still pending after it) is unknown, never a fabricated
            # empty-jobs "no CI activity here" reading.
            unknown_ids.add(raw["id"])
            budget_skipped.append((repo, raw["id"]))
            continue
        fut = pool.submit(paginate, _repo_path(repo, f"actions/runs/{raw['id']}/jobs?per_page=100"), ".jobs")
        futures[fut] = raw

    for fut in as_completed(futures):
        raw = futures[fut]
        try:
            jobs = [normalize_job(j, now) for j in fut.result()]
        except Exception as exc:  # one run failing must not sink the pass
            msg = str(exc)
            lst = errors.setdefault("jobs", [])
            if msg not in lst:
                lst.append(msg)
            unknown_ids.add(raw["id"])
            continue
        jobs_by_run[raw["id"]] = jobs
        if raw.get("status") == "completed":
            cache.put(raw["id"], jobs)
    return jobs_by_run, unknown_ids


def collect_repo(repo, fetch, paginate, cache, now, local_dirs, pool,
                  deadline=None, time_fn=_time.time, budget_skipped=None):
    """Per-repo section. Each of runners/prs/runs is fetched independently: a failure in
    one must never blank the others, and the real error text is preserved in `errors`
    rather than a fabricated default (see build_snapshot for the same rule on rate_limit).

    `deadline`/`time_fn`/`budget_skipped` implement the per-pass wall-clock budget:
    `deadline` is an absolute time_fn() value past which no new jobs fetch is issued,
    and `budget_skipped` is a list (owned by the caller, shared across repos) that this
    call appends (repo, run_id) onto for every run cut by the budget, so build_snapshot
    can report how many runs were not fetched."""
    if budget_skipped is None:
        budget_skipped = []
    errors = {}
    raw_runners, raw_prs, raw_runs = [], [], []
    try:
        raw_runners = (fetch(["api", _repo_path(repo, "actions/runners")]) or {}).get("runners", [])
    except Exception as exc:
        errors["runners"] = str(exc)
    try:
        raw_prs = paginate(_repo_path(repo, "pulls?state=open&per_page=100"), ".")
    except Exception as exc:
        errors["prs"] = str(exc)
    shas = [p["head"]["sha"] for p in raw_prs if p.get("head", {}).get("sha")]
    raw_runs = _fetch_runs(repo, fetch, shas, errors)
    jobs_by_run, unknown_ids = _fetch_jobs_for(
        repo, raw_runs, paginate, cache, now, pool, errors, deadline, time_fn, budget_skipped
    )
    runs = [
        {**normalize_run(raw, jobs_by_run.get(raw["id"], [])), "jobs_unknown": raw["id"] in unknown_ids}
        for raw in raw_runs
    ]
    runs_by_sha = {}
    for run in runs:
        runs_by_sha.setdefault(run["head_sha"], []).append(run)
    prs = [build_pr(raw, runs_by_sha.get(raw["head"]["sha"], [])) for raw in raw_prs]
    prs.sort(key=lambda p: p["number"], reverse=True)
    pr_by_sha = {p["sha"]: p["number"] for p in prs}
    return {
        "runners": attach_runner_jobs(raw_runners, runs, repo, local_dirs),
        "prs": prs,
        "deploys": select_deploys(runs, repo),
        "queue": build_queue(runs, pr_by_sha),
        "errors": errors,
    }


def build_snapshot(fetch, paginate, cache, local_probe, now, pool, time_fn=_time.time):
    """Full snapshot. `rate_limit` is fetched independently of the per-repo passes: if it
    fails, `remaining` stays None (never 0 or a guessed value) and the real error text
    becomes the banner. `rate_limit.reset_at` is converted to a UTC ISO 8601 string,
    consistent with `fetched_at`, rather than left as a raw epoch integer -- but only
    when it was actually retrieved; a failed call leaves it None, never a guessed
    timestamp. A section failure inside one repo (e.g. runners wedged) must not blank
    another repo's or another section's data -- collect_repo isolates errors per
    section, and this loop still runs every repo even if an earlier one had errors.

    `local_probe` is `{"checked_at", "error", "dirs"}` as produced by server.py's local
    probe loop (or a static disabled stand-in `{"dirs": None, ...}` when no probe is
    configured) -- `dirs=None` propagates through attach_runner_jobs as "no local-check
    data available", which is exactly None/unknown, never a fabricated dead runner.

    The whole pass is given a `PASS_BUDGET_S` wall-clock budget. Once it is exceeded, no
    further jobs fetches are issued; every run cut by the budget is marked unknown
    (never a fabricated "no jobs"), and the snapshot carries `partial=True` plus a
    message naming how many runs were not fetched."""
    t0 = time_fn()
    deadline = t0 + PASS_BUDGET_S
    banner = None
    rate = {"remaining": None, "reset_at": None}
    try:
        core = (fetch(["api", "rate_limit"]) or {})["resources"]["core"]
        reset_at = datetime.fromtimestamp(core["reset"], timezone.utc).isoformat()
        rate = {"remaining": core["remaining"], "reset_at": reset_at}
    except Exception as exc:
        banner = f"GitHub API problem: {exc}"
    repos = {}
    budget_skipped = []
    for repo in REPOS:
        repos[repo] = collect_repo(
            repo, fetch, paginate, cache, now, local_probe.get("dirs"), pool,
            deadline=deadline, time_fn=time_fn, budget_skipped=budget_skipped,
        )
        for key, msgs in repos[repo]["errors"].items():
            texts = msgs if isinstance(msgs, list) else [msgs]
            for msg in texts:
                low = msg.lower()
                if "rate limit" in low:
                    banner = f"GitHub rate limit exceeded; resets at {rate['reset_at']}"
                elif "auth" in low or "credentials" in low:
                    banner = "gh authentication expired: run `gh auth login`"
    snap = {
        "fetched_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "pass_seconds": round(time_fn() - t0, 1),
        "rate_limit": rate,
        "banner": banner,
        "owner": OWNER,
        "local_probe": local_probe,
        "repos": repos,
        "partial": False,
    }
    if budget_skipped:
        snap["partial"] = True
        snap["partial_message"] = (
            f"{len(budget_skipped)} run(s) not fetched within the {PASS_BUDGET_S}s pass budget"
        )
    return snap
