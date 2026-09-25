# CI Dashboard

A small, self-hosted dashboard for a GitHub Actions fleet: self-hosted runners, open
pull requests, and deploy workflows, on one page, refreshed automatically.

## The problem

A self-hosted GitHub Actions runner can report `status: online, busy: true` in the
GitHub API while executing nothing at all -- a wedged process, a crashed listener that
never re-registered, a runner stuck between jobs. GitHub's own UI has no way to show
you that: it shows the runner as busy, full stop. Cross-referencing "busy according to
GitHub" against "is there actually an in-progress job assigned to this runner" (and,
optionally, against a real local process listing) is the only way to catch that state,
and nobody wants to do it by hand every time a queue looks stuck.

Separately, a dashboard that builds its own payload inside the HTTP request handler
hangs exactly when you need it most: a full pass over a busy org's runs, pull requests,
and jobs can take on the order of a minute, and if that work happens on the request
thread, the page becomes unusable during the one window where you're trying to
diagnose something. This dashboard never does that -- see **How it works** below.

## What it shows

- **Fleet** -- registered self-hosted runners per repo (online/offline, busy, and
  optionally a local-process cross-check), the current queue of jobs waiting for a
  runner, and a runner-restart control (opt-in, see below).
- **PRs** -- every open pull request per repo, its CI rollup state, and a row of
  category chips (lint / typecheck / tests / sentinels / build / deploy / perf /
  visual) summarizing the worst state of that category's steps across all of the PR's
  runs.
- **Deploys** -- recent runs of the workflows you've named as deploy workflows,
  restricted to pushes to `main` (never a pull request run), most recent first.
- **GitHub status strip** -- the components of githubstatus.com this dashboard depends
  on (API Requests, Actions, Pull Requests, Webhooks, Git Operations), plus any open
  incident. When a section fails *and* GitHub reports the matching component degraded,
  the banner says so, so an outage isn't mistaken for a problem with your CI.
- **Header** -- snapshot age, API quota left and how many `gh` calls the last pass made,
  and, if configured, webhook delivery status. A red banner appears if the collector
  thread has died or its data is overdue.

## How it works

The process runs four threads:

- **Collector thread** -- on a timer (`poll_seconds`, default 300s), calls the GitHub
  API (via the `gh` CLI) for every configured repo's runners, open pull requests, and
  workflow runs, normalizes it all into one JSON snapshot, and swaps it into shared
  state under a lock. This is the only thread that ever calls the GitHub API for the
  main dashboard data, and the only thing that can be slow.
- **HTTP thread(s)** -- serve `GET /api/status` by reading the last completed snapshot
  under that same lock and returning it immediately. They never build a snapshot
  themselves. This is why `/api/status` is instant even while a fresh pass is running:
  the two are fully decoupled. A `POST /api/refresh` just wakes the collector thread up
  early; it does not block on the result.
- **Local-probe thread** (optional, see below) -- on its own timer and its own
  timeout, independent of the collector, so a wedged probe command can only ever stall
  itself.
- **GitHub-status thread** -- polls `https://www.githubstatus.com/api/v2/summary.json`
  every `status_poll_seconds`. That feed is public and outside the GitHub REST API, so
  it costs no API quota. If it can't be read, the strip shows the last reading marked
  stale (or "unknown"), never green.

### Keeping API use down

- **Head-sha cache.** Each open PR's runs are looked up by head sha, because the recent
  runs listing can miss an older PR's only run. Once all of a sha's runs have completed,
  the result is cached (`sha_cache_ttl_seconds`, default 15 min) and not re-queried.
  The cache is bypassed for any sha with an active run, on a repo's first pass, when
  the recent runs listing failed, and when that listing is full of runs newer than the
  previous pass (GitHub lists runs newest first, so a run may have been pushed off the
  page). A run that shows up in the queued/in-progress listings (for example a re-run)
  updates the cached copy, so its sha is queried again next pass.
- **Completed-run jobs cache.** A completed run's jobs never change, so they are
  fetched once (unchanged from before).
- **Refresh debounce.** "Refresh now" is refused (HTTP 429, with the wait time) within
  `min_refresh_seconds` of the last pass, and does nothing while a pass is running.
- **Backoff.** After each pass, the next one is pushed back when: API quota left is
  below 3x what the last pass used (waits for the reset), a primary or secondary rate
  limit was hit, or GitHub reports a major outage of API Requests or Actions. The
  reason is shown in the banner.
- **Call count.** The header shows how many `gh` invocations the last pass made. A
  paginated call can be several HTTP requests, so treat it as a floor.

### Webhooks (optional)

Set `webhook_secret` and point a GitHub webhook (content type `application/json`, same
secret) at `POST /api/webhook`. Subscribe to **Workflow runs**, **Workflow jobs** and
**Pull requests**. The server binds to `127.0.0.1` only, so GitHub needs a tunnel or a
local forwarder to reach it. This dashboard doesn't expose itself to the internet.

- Every delivery must carry a valid `X-Hub-Signature-256`; anything else gets 401.
- A webhook is only a *hint*: it triggers an early partial pass of the named repo
  (coalesced, and never closer than `min_refresh_seconds` to the last pass). The page
  never shows webhook payload data; it always re-reads from the API.
- While webhooks are demonstrably arriving, full passes slow to
  `webhook_sweep_seconds` (default 30 min) as a safety sweep. If a full pass finds a
  run created well after the last delivery with no delivery since, the dashboard
  reports "missing deliveries" and goes back to `poll_seconds`.

Each repo's runners, pull requests, and workflow runs are fetched independently, and a
failure in one (a `gh` error, a rate limit, an auth expiry) is recorded per-section and
surfaced in the UI banner rather than blanking the rest of the snapshot. A pull
request's CI rollup distinguishes "no CI at all" (`no_ci`) from "we could not retrieve
part of its CI state" (`unknown`) -- an unknown never silently reads as green.

## Requirements

- Python 3.11+ (standard library only for the server; `pytest` for the test suite).
- The [`gh` CLI](https://cli.github.com/), authenticated (`gh auth login`) as a user or
  token with read access to the repos you configure (and, if you want the restart
  feature, permission to re-run workflow runs).
- Self-hosted runners are optional. The Fleet tab and the local-process probe are only
  useful if you have some; PRs and Deploys work against GitHub-hosted runners too.

## Install and run

```bash
git clone <this repo>
cd ci-dashboard
cp config.example.json config.json
# edit config.json: set "owner" and "repos" at minimum
gh auth status   # confirm gh is authenticated
python server.py
```

Then open `http://localhost:8787` (or whatever `port` you configured).

`config.json` is gitignored on purpose -- it is the one file that holds your specific
repos and, if you enable it, your restart command. Never commit it.

## Configuration reference

All settings live in `config.json`, loaded once at startup (`config.py`). Copy
`config.example.json` to `config.json` and edit it. Only `owner` and `repos` are
required; everything else has the default shown.

| Key | Default | Meaning |
|---|---|---|
| `owner` | *(required)* | GitHub user or organization that owns the repos. |
| `repos` | *(required)* | List of repo names (under `owner`) to track. |
| `deploy_workflows` | `{}` | Map of repo name to a list of workflow file paths (e.g. `.github/workflows/deploy.yml`) shown on the Deploys tab. A repo with no entry shows no deploys. |
| `runner_local_dirs` | `{}` | Optional map, `{repo: {runner_name: local_dir_name}}`, used together with `local_probe` (below) to cross-check a GitHub-reported runner against a real local process. |
| `local_probe` | `null` | Optional local-process probe config (see below). Absent means the probe never runs. |
| `restart_command` | `null` | Optional argv list to fire when an operator requests a runner restart (see below). Absent means the feature is entirely disabled. |
| `port` | `8787` | HTTP port. |
| `poll_seconds` | `300` | Seconds between collector passes. |
| `pass_budget_seconds` | `150` | Wall-clock budget for one collector pass; runs not fetched within it are marked `unknown`, never fabricated as empty/green, and the snapshot is flagged `partial`. |
| `jobs_pool_workers` | `8` | Thread pool size for concurrent per-run jobs fetches. |
| `gh_timeout_seconds` | `20` | Timeout for each `gh api` call. |
| `actions_log_path` | `"actions.log"` | Where restart-related audit entries are appended. Relative paths resolve against the project directory. |
| `min_refresh_seconds` | `30` | Minimum gap between the end of one pass and a manual or webhook-triggered pass. |
| `sha_cache_ttl_seconds` | `900` | Longest a finished PR's runs are served from the head-sha cache before being re-queried. |
| `status_poll_seconds` | `120` | How often githubstatus.com is polled. `0` disables the GitHub status strip. |
| `gh_status_components` | `null` | Component names to show from githubstatus.com. `null` uses API Requests, Actions, Pull Requests, Webhooks, Git Operations. A name not in the feed shows as "not in feed". |
| `webhook_secret` | `null` | Enables `POST /api/webhook` and verifies deliveries with it. `null` disables webhooks (404). |
| `webhook_sweep_seconds` | `1800` | Full-pass interval while webhooks are arriving. |

Environment variables override the corresponding config values for convenience:
`CI_DASH_CONFIG` (path to the config file itself, default `config.json` next to
`server.py`), `CI_DASH_PORT`, `CI_DASH_INTERVAL`, `CI_DASH_LOG_PATH`,
`CI_DASH_MIN_REFRESH`, `CI_DASH_GH_STATUS_URL`, and `CI_DASH_NO_GH_STATUS=1` (disables
the status thread).

### Monitoring the dashboard itself

`GET /api/health` returns `200` with `{"ok": true, ...}` when the collector thread is
alive, its last pass succeeded, and the next pass isn't overdue; otherwise `503` with the
reason in `collector`.

### The local-process probe

GitHub's runner API can only tell you what GitHub believes; it cannot tell you whether
a runner process is actually alive on the machine that's supposed to be running it. If
you have shell access to your runner hosts, you can optionally configure a command that
lists local runner processes, and a regex to pull the runner's directory name out of
each line:

```json
"local_probe": {
  "command": ["wsl", "-d", "Ubuntu", "-u", "root", "--", "pgrep", "-af", "Runner.Listener"],
  "pattern": "/root/([\\w.-]+)/bin/Runner\\.Listener"
},
"runner_local_dirs": {
  "hello-world": {"runner-1": "hello-world-runner"}
}
```

`pattern` must have exactly one capture group naming the runner's local directory;
`runner_local_dirs` then maps each GitHub-reported `(repo, runner name)` to the
directory name you expect to see. This example happens to reach into a WSL (Windows
Subsystem for Linux) distro, because that's a common way to run a self-hosted runner on
Windows -- but `command` can be anything: SSH into a remote host, `ps` on the local
machine, whatever fits your setup. **This is inherently platform- and
setup-specific, which is why it's off by default.**

When `local_probe` is absent, the probe command never runs, and every runner's
`local_alive` field is `null` (unknown) in the UI -- never `false`. A missing probe must
never be misread as "runner confirmed dead."

### The restart button

The Fleet tab can offer a button that fires a configured command to restart your
runner fleet (e.g. re-running a Windows Scheduled Task, restarting a systemd service,
re-launching a container). This is **entirely platform-specific** -- there is no
portable way to "restart a self-hosted runner" that works the same way on every OS and
deployment, so you must supply the exact command for your own setup:

```json
"restart_command": ["schtasks", "/run", "/tn", "MyRunnerRestartTask"]
```

When `restart_command` is absent (the default), the feature is fully disabled: the
restart buttons do not render, and `POST /api/restart` refuses with `400` explaining
that it isn't configured. The dashboard offers three restart modes when it is enabled:

- **Restart when idle** -- waits for every runner to go idle, then fires the command.
- **Restart now** -- fires immediately, tracking any jobs that were interrupted and
  automatically re-running them once the runners come back online.
- **Force now** -- available once a "restart when idle" request has stalled (no runner
  went idle within 20 minutes); fires immediately, same as "Restart now".

Firing the restart command and re-running interrupted jobs are the only two ways this
tool ever writes to your GitHub org or your infrastructure -- see Limitations below.

## Limitations

- **Polling, with optional webhook hints.** The dashboard reflects the state as of its
  last collector pass (`poll_seconds`, default every 5 minutes, or on-demand via
  "Refresh now"). Webhooks, if configured, only trigger earlier passes; they are never
  displayed as state.
- **Read-only except for two things.** The only writes this tool ever makes are firing
  your configured `restart_command` and calling GitHub's re-run-failed-jobs API for
  runs interrupted by a restart -- both only when you explicitly enable and trigger the
  restart feature. Everything else is read-only against the GitHub API.
- **A full pass is not instant.** Fetching runners, every open PR, and every relevant
  workflow run's jobs, for a busy org, takes on the order of a minute in practice. The
  dashboard is designed around this (see How it works) rather than around making the
  fetch itself faster, and a pass that runs long is capped (`pass_budget_seconds`) and
  reported as partial rather than left to run indefinitely.
- **Category chips are name-heuristic, not semantic.** The lint/typecheck/tests/
  sentinels/build/deploy/perf/visual categorization is a regex match against step and
  job names (see `collector.py`'s `CATEGORY_PATTERNS`), tuned against common naming
  conventions. It will misclassify a step whose name doesn't match any pattern (falling
  back to an "other" bucket that's hidden from the chip row) or, rarely, match the
  wrong one.
- **Steps aren't available until a job starts.** GitHub does not publish a job's step
  list until the job has actually started running, so a still-queued job has no step
  data yet. The dashboard falls back to categorizing the *job's own name* in that case,
  which is coarser than per-step categorization but keeps a queued job from silently
  vanishing from the category chips.
