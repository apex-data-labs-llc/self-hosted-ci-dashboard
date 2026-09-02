import re as _re
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.conftest import load
import collector as C


@pytest.mark.parametrize("name,expected", [
    ("ESLint", "lint"),
    ("Lint", "lint"),
    ("Stylelint (token conformance)", "lint"),
    ("ruff check", "lint"),
    ("Typecheck", "typecheck"),
    ("mypy", "typecheck"),
    ("Build (includes typecheck via `tsc -b`)", "typecheck"),
    ("Run pytest shard", "tests"),
    ("Test", "tests"),
    ("Sentinel reachability", "sentinels"),
    ("Deploy staging", "deploy"),
    ("Build", "build"),
    ("Run actions/checkout@v4", "setup"),
    ("Set up job", "setup"),
    ("Post Run actions/checkout@v4", "setup"),
    ("npm ci", "setup"),
    ("Something unusual", "other"),
    ("Complete job", "setup"),
    ("Bundle size budget", "perf"),
    ("Lighthouse CI (home page only)", "perf"),
    ("Visual (mobile viewports)", "visual"),
    ("Vitest", "tests"),  # \btests?\b needs a word boundary "Vitest" lacks
    ("npm audit (high+ = fail)", "sentinels"),  # dependency/security gate
    ("verify_modularity (structural guard)", "sentinels"),
    ("Verify results", "other"),  # genuinely uncategorizable, see ALLOWED_OTHER
    # Narrowing setup's bare \binstall\b / \bcache\b tokens must not let a
    # category-specific step that merely mentions "install"/"cache" mid-string get
    # swallowed by setup ahead of its real category.
    ("Warm build cache", "build"),
    ("Run npm install for linter", "lint"),
    # sentinels ordered ahead of tests: a guard/sweep step whose name happens to
    # contain the literal word "tests" must still read as sentinels.
    ("Dependency guard sweep (repo-root tests/)", "sentinels"),
    ("Sentinel + scaffold tests", "sentinels"),
    ("Run pytest", "tests"),
    ("Scripts tests", "tests"),
    ("Compute test set", "tests"),
])
def test_categorize_step(name, expected):
    assert C.categorize_step(name) == expected


def test_every_real_ui_step_is_categorized_non_other():
    jobs = load("jobs_ui.json")["jobs"]
    names = [s["name"] for j in jobs for s in j["steps"]]
    assert names, "fixture has steps"
    others = [n for n in names if C.categorize_step(n) == "other"]
    assert others == [], f"uncategorized real steps: {others}"


@pytest.mark.parametrize("fixture", [
    "jobs_ui.json",
    "jobs_failed.json",
    "jobs_skipped.json",
    "jobs_queued.json",
])
def test_every_real_step_is_categorized_or_explicitly_allowed_other(fixture):
    """Every step name across all four fixtures must land in a real category, except
    the explicit, justified entries in collector.ALLOWED_OTHER. This stays strict about
    everything not on that list -- a new "other" step name fails this test until either
    the table gains a pattern for it or it is added to ALLOWED_OTHER with a reason.
    """
    jobs = load(fixture)["jobs"]
    names = [s["name"] for j in jobs for s in j["steps"]]
    unexpected_others = [
        n for n in names
        if C.categorize_step(n) == "other" and n not in C.ALLOWED_OTHER
    ]
    assert unexpected_others == [], f"uncategorized real steps not in ALLOWED_OTHER: {unexpected_others}"


def test_normalize_job_queued_measures_wait_from_created():
    raw = load("jobs_queued.json")["jobs"][0]
    assert raw["status"] == "queued"
    now = C.parse_ts(raw["created_at"]) + 600
    job = C.normalize_job(raw, now)
    assert job["queued_s"] == 600
    assert job["duration_s"] is None
    assert job["steps"] == [] or all("category" in s for s in job["steps"])


def test_normalize_job_completed_has_duration():
    raw = next(j for j in load("jobs_failed.json")["jobs"] if j["conclusion"] == "failure")
    job = C.normalize_job(raw, time.time())
    assert job["duration_s"] is not None and job["duration_s"] >= 0
    assert job["conclusion"] == "failure"


def test_normalize_run_carries_jobs():
    raw = load("runs.json")["workflow_runs"][0]
    run = C.normalize_run(raw, jobs=[{"job_id": 1}])
    assert run["run_id"] == raw["id"] and run["jobs"] == [{"job_id": 1}]


def _job(status, conclusion=None, steps=()):
    return {"status": status, "conclusion": conclusion,
            "steps": [{"name": n, "status": st, "conclusion": c, "category": C.categorize_step(n)} for n, st, c in steps]}


def test_job_state_table():
    assert C.job_state(_job("completed", "failure")) == "failure"
    assert C.job_state(_job("completed", "cancelled")) == "failure"
    assert C.job_state(_job("in_progress")) == "running"
    assert C.job_state(_job("queued")) == "queued"
    assert C.job_state(_job("waiting")) == "queued"
    assert C.job_state(_job("completed", "skipped")) == "skipped"
    assert C.job_state(_job("completed", "success")) == "success"
    assert C.job_state(_job("completed", "neutral")) == "success"


def test_rollup_priority():
    assert C.rollup([]) == "no_ci"
    assert C.rollup(["success", "queued", "failure"]) == "failure"
    assert C.rollup(["queued", "running"]) == "running"
    assert C.rollup(["queued", "skipped"]) == "queued"
    assert C.rollup(["skipped", "skipped"]) == "skipped"
    assert C.rollup(["skipped", "success"]) == "success"


def test_rollup_unknown_outranks_every_optimistic_state():
    assert C.rollup(["unknown", "success", "success"]) == "unknown"
    assert C.rollup(["unknown", "running"]) == "unknown"
    assert C.rollup(["unknown", "queued"]) == "unknown"
    # failure still outranks unknown -- a definite failure is still definite.
    assert C.rollup(["unknown", "failure"]) == "failure"


def test_all_skipped_is_not_success():
    jobs = [C.normalize_job(j, 0) for j in load("jobs_skipped.json")["jobs"]]
    assert C.rollup([C.job_state(j) for j in jobs]) == "skipped"


def test_queued_only_run_rolls_up_queued_not_running():
    jobs = [C.normalize_job(j, 0) for j in load("jobs_queued.json")["jobs"]]
    states = [C.job_state(j) for j in jobs]
    assert "running" not in states
    assert C.rollup(states) == "queued"


def test_counts():
    jobs = [_job("completed", "success"), _job("in_progress"), _job("queued"),
            _job("completed", "failure"), _job("completed", "skipped")]
    assert C.counts(jobs) == {"executed": 2, "running": 1, "queued": 1, "failed": 1, "skipped": 1}


def test_chips_worst_state_per_visible_category_hides_setup():
    jobs = [_job("completed", "failure", steps=[
        ("Run actions/checkout@v4", "completed", "success"),
        ("Lint", "completed", "failure"),
        ("Test", "completed", "success"),
    ]), _job("queued", steps=[("pytest shard 2", "queued", None)])]
    assert C.chips(jobs) == {"lint": "failure", "tests": "queued"}


def test_build_pr_shape():
    raw_pr = load("pulls.json")[0]
    jobs = [C.normalize_job(j, 0) for j in load("jobs_failed.json")["jobs"]]
    run = C.normalize_run(load("runs.json")["workflow_runs"][0], jobs)
    pr = C.build_pr(raw_pr, [run])
    assert set(pr) >= {"number", "title", "branch", "sha", "url", "rollup", "counts", "chips", "runs"}
    assert pr["rollup"] == "failure"


def test_build_pr_reports_unknown_when_one_run_is_jobs_unknown_alongside_a_success():
    # jobs_unknown is set on the run dict exactly the way collect_repo's normalize_run
    # output sets it, not by hand-passing the string "unknown" into rollup, so this
    # proves the wiring (build_pr -> rollup) rather than just the rollup comparison in
    # isolation.
    raw_pr = load("pulls.json")[0]
    sha = raw_pr["head"]["sha"]
    successful_run = {
        "run_id": 1, "workflow": "CI", "head_sha": sha, "url": "u1",
        "jobs": [_job("completed", "success")], "jobs_unknown": False,
    }
    unknown_run = {
        "run_id": 2, "workflow": "CI", "head_sha": sha, "url": "u2",
        "jobs": [], "jobs_unknown": True,
    }
    pr = C.build_pr(raw_pr, [successful_run, unknown_run])
    assert pr["rollup"] == "unknown"


def test_parse_local_probe_extracts_dirs_and_ignores_garbage():
    text = load("local_probe_sample.txt")
    pattern = _re.compile(r"/root/([\w.-]+)/bin/Runner\.Listener")
    assert C.parse_local_probe(text, pattern) == {"runner-1", "runner-2"}
    assert C.parse_local_probe("", pattern) == set()


def test_build_queue_oldest_first_with_pr_link():
    j1 = {"job_id": 1, "name": "a", "status": "queued", "conclusion": None, "created_at": "2026-09-02T10:00:00Z", "queued_s": 600, "url": "u1", "steps": []}
    j2 = {"job_id": 2, "name": "b", "status": "queued", "conclusion": None, "created_at": "2026-09-02T10:05:00Z", "queued_s": 300, "url": "u2", "steps": []}
    j3 = {"job_id": 3, "name": "c", "status": "in_progress", "conclusion": None, "created_at": "2026-09-02T10:05:00Z", "queued_s": 1, "url": "u3", "steps": []}
    run = {"run_id": 7, "workflow": "Tests", "head_sha": "abc", "url": "ru", "jobs": [j2, j1, j3]}
    q = C.build_queue([run], {"abc": 1660})
    assert [x["job_id"] for x in q] == [1, 2]
    assert q[0]["pr_number"] == 1660 and q[0]["waited_s"] == 600 and q[0]["workflow"] == "Tests"


def test_attach_runner_jobs_marks_alive_and_current():
    runners = [{"name": "runner-1", "status": "online", "busy": True},
               {"name": "runner-2", "status": "online", "busy": False}]
    running = {"job_id": 5, "name": "pytest", "status": "in_progress", "conclusion": None, "runner_name": "runner-1", "started_at": "2026-09-02T10:00:00Z", "url": "u", "steps": []}
    runs = [{"run_id": 1, "workflow": "Tests", "head_sha": "x", "url": "r", "jobs": [running]}]
    out = C.attach_runner_jobs(runners, runs, "hello-world", {"runner-1"})
    assert out[0]["local_alive"] is True and out[0]["current_job"]["job_id"] == 5
    assert out[1]["local_alive"] is False and out[1]["current_job"] is None
    out2 = C.attach_runner_jobs(runners, runs, "hello-world", None)
    assert out2[0]["local_alive"] is None


def test_attach_runner_jobs_busy_but_no_in_progress_job_is_the_wedge_signature():
    # The distinguishing signal of a wedged runner: status=online, busy=True with no
    # in_progress job naming the runner. The honest output is current_job=None, not a
    # "probably running" guess.
    runners = [{"name": "runner-1", "status": "online", "busy": True},
               {"name": "runner-2", "status": "online", "busy": True}]
    queued_job = {"job_id": 99, "name": "pytest", "status": "queued", "conclusion": None,
                  "runner_name": None, "created_at": "2026-09-02T08:00:00Z", "url": "u", "steps": []}
    runs = [{"run_id": 1, "workflow": "Tests", "head_sha": "x", "url": "r", "jobs": [queued_job]}]
    out = C.attach_runner_jobs(runners, runs, "hello-world", {"runner-1", "runner-2"})
    by_name = {r["name"]: r for r in out}
    assert by_name["runner-1"]["busy"] is True
    assert by_name["runner-1"]["current_job"] is None
    assert by_name["runner-2"]["busy"] is True
    assert by_name["runner-2"]["current_job"] is None


def test_select_deploys_only_main_branch_deploy_workflows():
    runs = [
        {"run_id": 1, "path": ".github/workflows/deploy.yml", "head_branch": "main", "event": "push", "created_at": "2026-09-02T10:00:00Z", "jobs": []},
        {"run_id": 2, "path": ".github/workflows/test.yml", "head_branch": "main", "event": "push", "created_at": "2026-09-02T10:01:00Z", "jobs": []},
        {"run_id": 3, "path": ".github/workflows/deploy.yml", "head_branch": "feat/x", "event": "pull_request", "created_at": "2026-09-02T10:02:00Z", "jobs": []},
        {"run_id": 4, "path": ".github/workflows/rollback.yml", "head_branch": "main", "event": "workflow_dispatch", "created_at": "2026-09-02T10:03:00Z", "jobs": []},
    ]
    assert [r["run_id"] for r in C.select_deploys(runs, "hello-world")] == [4, 1]
    ui = [{"run_id": 9, "path": ".github/workflows/ci.yml", "head_branch": "main", "event": "push", "created_at": "2026-09-02T10:00:00Z", "jobs": []}]
    assert [r["run_id"] for r in C.select_deploys(ui, "spoon-knife")] == [9]


def test_build_queue_unknown_wait_yields_none_and_sorts_first():
    # job dict with no created_at at all, so normalize_job's queued_s computation
    # (which requires created_at) genuinely cannot produce a value.
    raw_job = {"id": 42, "run_id": 7, "name": "mystery", "status": "queued", "conclusion": None}
    job = C.normalize_job(raw_job, now=1000.0)
    assert job["queued_s"] is None  # confirms the None flows in from normalize_job, not hand-set

    known_job = {"job_id": 1, "name": "a", "status": "queued", "conclusion": None,
                 "created_at": "2026-09-02T10:00:00Z", "queued_s": 600, "url": "u1", "steps": []}
    unknown_job = {**job, "job_id": job["job_id"]}
    run = {"run_id": 7, "workflow": "Tests", "head_sha": "abc", "url": "ru", "jobs": [known_job, unknown_job]}
    q = C.build_queue([run], {})
    assert q[0]["job_id"] == 42
    assert q[0]["waited_s"] is None
    assert q[1]["job_id"] == 1
    assert q[1]["waited_s"] == 600


def test_build_queue_mixed_known_and_unknown_waits_orders_unknown_first_then_oldest():
    raw_missing = {"id": 5, "run_id": 1, "name": "no-created-at", "status": "queued", "conclusion": None}
    missing_job = C.normalize_job(raw_missing, now=1000.0)
    assert missing_job["queued_s"] is None

    j_old = {"job_id": 1, "name": "old", "status": "queued", "conclusion": None,
             "created_at": "2026-09-02T10:00:00Z", "queued_s": 600, "url": "u1", "steps": []}
    j_new = {"job_id": 2, "name": "new", "status": "queued", "conclusion": None,
             "created_at": "2026-09-02T10:05:00Z", "queued_s": 300, "url": "u2", "steps": []}
    run = {"run_id": 1, "workflow": "Tests", "head_sha": "abc", "url": "ru",
           "jobs": [j_new, missing_job, j_old]}
    q = C.build_queue([run], {})
    assert [x["job_id"] for x in q] == [5, 1, 2]
    assert q[0]["waited_s"] is None


def test_select_deploys_excludes_pull_request_event_even_on_main_branch():
    runs = [
        {"run_id": 1, "path": ".github/workflows/deploy.yml", "head_branch": "main", "event": "pull_request", "created_at": "2026-09-02T10:00:00Z", "jobs": []},
        {"run_id": 2, "path": ".github/workflows/deploy.yml", "head_branch": "main", "event": "push", "created_at": "2026-09-02T10:01:00Z", "jobs": []},
    ]
    assert [r["run_id"] for r in C.select_deploys(runs, "hello-world")] == [2]


class FakeGh:
    """Serves fixtures by path prefix and records every call.

    `extra_runs`: runs that exist ONLY when queried by `head_sha=<sha>` -- i.e. outside
    the 50-run recency window and the queued/in_progress listings -- so a test can
    prove a PR's run is fetched via the per-PR head_sha query rather than silently
    missed."""
    def __init__(self, fail=None, extra_runs=None):
        self.calls = []
        self.fail = fail or {}
        self.runs = load("runs.json")["workflow_runs"]
        self.extra_runs = extra_runs or []

    def fetch(self, args):
        path = args[1]
        self.calls.append(path)
        for prefix, exc in self.fail.items():
            if path.startswith(prefix):
                raise exc
        if path == "rate_limit":
            return load("rate_limit.json")
        if path.endswith("/actions/runners"):
            return load("runners.json")
        if "/actions/runs?" in path:
            m = _re.search(r"head_sha=([0-9a-f]+)", path)
            if m:
                sha = m.group(1)
                pool = self.runs + self.extra_runs
                return {"workflow_runs": [r for r in pool if r.get("head_sha") == sha]}
            return {"workflow_runs": self.runs}
        raise AssertionError(f"unexpected fetch {path}")

    def paginate(self, path, jq):
        self.calls.append(path)
        for prefix, exc in self.fail.items():
            if path.startswith(prefix):
                raise exc
        if "/pulls?" in path:
            return load("pulls.json")
        if "/jobs" in path:
            return load("jobs_queued.json")["jobs"]
        raise AssertionError(f"unexpected paginate {path}")


def _collect(fake, cache=None):
    cache = cache or C.RunCache()
    with ThreadPoolExecutor(max_workers=4) as pool:
        return C.collect_repo("hello-world", fake.fetch, fake.paginate, cache, time.time(), None, pool), cache


def test_collect_repo_produces_all_sections():
    section, _ = _collect(FakeGh())
    assert set(section) == {"runners", "prs", "deploys", "queue", "errors"}
    assert section["errors"] == {}
    assert section["prs"] and all("rollup" in p for p in section["prs"])


def test_collect_repo_caches_completed_runs():
    fake = FakeGh()
    for r in fake.runs:
        r["status"] = "completed"
    _, cache = _collect(fake)
    jobs_calls = [c for c in fake.calls if "/jobs" in c]
    fake2 = FakeGh()
    for r in fake2.runs:
        r["status"] = "completed"
    with ThreadPoolExecutor(max_workers=4) as pool:
        C.collect_repo("hello-world", fake2.fetch, fake2.paginate, cache, time.time(), None, pool)
    assert jobs_calls and [c for c in fake2.calls if "/jobs" in c] == []


def test_collect_repo_section_errors_are_isolated():
    import gh_client as G
    fake = FakeGh(fail={"repos/octocat/hello-world/actions/runners": G.GhError("HTTP 403: API rate limit exceeded", "rate_limit")})
    section, _ = _collect(fake)
    assert "rate limit" in section["errors"]["runners"]
    assert section["prs"]  # still built


def test_run_cache_is_bounded():
    cache = C.RunCache(limit=2)
    cache.put(1, []); cache.put(2, []); cache.put(3, [])
    assert cache.get(1) is None and cache.get(3) == []


def test_build_snapshot_shape_and_banner():
    import gh_client as G
    fake = FakeGh(fail={"rate_limit": G.GhError("API rate limit exceeded", "rate_limit")})
    with ThreadPoolExecutor(max_workers=4) as pool:
        snap = C.build_snapshot(fake.fetch, fake.paginate, C.RunCache(), {"checked_at": None, "error": "not run", "dirs": None}, time.time(), pool)
    assert set(snap) >= {"fetched_at", "pass_seconds", "rate_limit", "banner", "owner", "repos"}
    assert set(snap["repos"]) == set(C.REPOS)
    assert snap["owner"] == "octocat"
    assert "rate limit" in (snap["banner"] or "").lower()


# ---------------------------------------------------------------------------
# Live-grounded findings: recency-window blind spot, jobs-fetch failure, queued
# jobs with no steps yet, pass-budget cutoff, rate-limit timestamp encoding.
# ---------------------------------------------------------------------------


def test_finding1_collect_repo_fetches_run_outside_recency_window_via_head_sha():
    # A PR head sha whose only run predates the 50-run recency window must still be
    # fetched, via a per-PR head_sha query, or no_ci fabricates "no CI" out of "we did
    # not look". old_run below exists ONLY via the head_sha query in FakeGh (see
    # extra_runs), never via the recency, queued, or in_progress listings, so this can
    # only pass if collect_repo actually issues that per-sha query.
    pulls = load("pulls.json")
    target = pulls[0]
    sha = target["head"]["sha"]
    old_run = {
        "id": 999999001,
        "name": "CI",
        "path": ".github/workflows/test.yml",
        "event": "push",
        "head_branch": target["head"]["ref"],
        "head_sha": sha,
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://example/old-run",
        "created_at": "2020-01-01T00:00:00Z",
        "run_attempt": 1,
    }
    fake = FakeGh(extra_runs=[old_run])
    section, _ = _collect(fake)
    pr = next(p for p in section["prs"] if p["number"] == target["number"])
    found = [r for r in pr["runs"] if r["run_id"] == 999999001]
    assert found, "old out-of-window run was never fetched"
    assert pr["rollup"] != "no_ci", "a PR with a real (if old) run must not read as no_ci"


def test_finding2_jobs_fetch_failure_yields_unknown_not_silent_success():
    # A jobs-fetch failure must read as unknown, never silently as success or no_ci
    # (which is what an empty jobs list would produce). Failing every jobs fetch means
    # every PR with a matched run must show unknown, and the error text must survive
    # into errors["jobs"]. A PR with rollup no_ci here genuinely has no matching run at
    # all, so the two states stay distinguishable even under total jobs-fetch failure.
    import gh_client as G
    fail_prefix = "repos/octocat/hello-world/actions/runs/"
    fake = FakeGh(fail={fail_prefix: G.GhError("HTTP 500: boom", "other")})
    section, _ = _collect(fake)
    assert section["errors"].get("jobs"), "jobs error must be recorded"
    assert any("boom" in m for m in section["errors"]["jobs"])
    assert any(p["rollup"] == "unknown" for p in section["prs"]), "no PR read as unknown"
    assert not any(p["rollup"] == "success" for p in section["prs"]), \
        "a fetch failure must never read as success"


def test_finding2_genuinely_empty_run_list_is_still_no_ci():
    # The negative case: a PR with a real, genuinely empty run list (no fetch failure
    # involved) must still produce no_ci, so no_ci and unknown stay distinguishable
    # rather than one silently swallowing the other.
    raw_pr = {"number": 1, "title": "t", "head": {"ref": "b", "sha": "nomatch"},
              "html_url": "u", "updated_at": None}
    pr = C.build_pr(raw_pr, [])
    assert pr["rollup"] == "no_ci"


def test_finding2_jobs_errors_accumulate_distinct_messages():
    # The broad case: multiple distinct jobs errors in one pass must all survive, not
    # just the first.
    errors = {}
    cache = C.RunCache()

    def flaky_paginate(path, jq):
        if path.endswith("/1/jobs?per_page=100"):
            raise RuntimeError("error A")
        if path.endswith("/2/jobs?per_page=100"):
            raise RuntimeError("error B")
        raise AssertionError("unexpected path")

    raw_runs = [
        {"id": 1, "status": "in_progress"},
        {"id": 2, "status": "in_progress"},
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs_by_run, unknown_ids = C._fetch_jobs_for(
            "hello-world", raw_runs, flaky_paginate, cache, time.time(), pool,
            errors, deadline=None, time_fn=time.time, budget_skipped=[],
        )
    assert unknown_ids == {1, 2}
    assert set(errors["jobs"]) == {"error A", "error B"}


def test_finding3_chips_non_empty_for_real_queued_jobs_with_no_steps():
    # GitHub does not populate steps until a job starts, so every queued job in the
    # real jobs_queued.json fixture has steps=[]. Before this fix chips() read only
    # step categories and returned {} for every such PR. It must now categorize the
    # job NAME and produce a non-empty chips dict.
    now = time.time()
    raw_jobs = load("jobs_queued.json")["jobs"]
    jobs = [C.normalize_job(j, now) for j in raw_jobs]
    assert all(j["steps"] == [] for j in jobs), "fixture assumption changed: jobs now have steps"
    run = {"run_id": 1, "workflow": "CI", "head_sha": "abc", "url": "u",
           "jobs": jobs, "jobs_unknown": False}
    raw_pr = {"number": 1, "title": "t", "head": {"ref": "b", "sha": "abc"},
              "html_url": "u", "updated_at": None}
    pr = C.build_pr(raw_pr, [run])
    assert pr["chips"], "chips must not be empty for real queued jobs"
    assert pr["chips"].get("sentinels") == "queued"


def test_finding4_build_snapshot_partial_when_pass_budget_exceeded():
    # Force the pass-budget clock to already be past deadline before any jobs fetch is
    # issued. Every run that would otherwise have been fetched must be marked unknown
    # (never fabricated as empty/green), and the snapshot must report partial=True with
    # a message naming how many runs were skipped.
    fake = FakeGh()
    calls = {"n": 0}

    def fake_time():
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 10_000.0

    with ThreadPoolExecutor(max_workers=4) as pool:
        snap = C.build_snapshot(
            fake.fetch, fake.paginate, C.RunCache(),
            {"checked_at": None, "error": None, "dirs": None}, time.time(), pool,
            time_fn=fake_time,
        )
    assert snap["partial"] is True
    assert "not fetched" in snap["partial_message"]
    all_prs = [p for repo in snap["repos"].values() for p in repo["prs"]]
    assert any(p["rollup"] == "unknown" for p in all_prs), "a budget-cut run must read as unknown"
    assert not any(p["rollup"] == "success" for p in all_prs), \
        "a budget-cut run must never read as success"


def test_finding5_rate_limit_reset_at_is_iso8601_utc():
    fake = FakeGh()
    with ThreadPoolExecutor(max_workers=4) as pool:
        snap = C.build_snapshot(
            fake.fetch, fake.paginate, C.RunCache(),
            {"checked_at": None, "error": None, "dirs": None}, time.time(), pool,
        )
    reset_at = snap["rate_limit"]["reset_at"]
    assert reset_at is not None
    from datetime import datetime as _dt
    parsed = _dt.fromisoformat(reset_at)
    assert parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def test_finding5_rate_limit_reset_at_stays_none_on_failure():
    # A failed rate_limit call must still leave reset_at (and remaining) as None, never
    # a fabricated/guessed ISO string.
    import gh_client as G
    fake = FakeGh(fail={"rate_limit": G.GhError("API rate limit exceeded", "rate_limit")})
    with ThreadPoolExecutor(max_workers=4) as pool:
        snap = C.build_snapshot(
            fake.fetch, fake.paginate, C.RunCache(),
            {"checked_at": None, "error": None, "dirs": None}, time.time(), pool,
        )
    assert snap["rate_limit"] == {"remaining": None, "reset_at": None}
