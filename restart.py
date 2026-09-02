"""Pure restart state machine. server.py executes the actions it returns."""
from dataclasses import dataclass, field, asdict

DRAIN_STALL_S = 1200
VERIFY_WINDOW_S = 300


@dataclass
class RestartState:
    state: str = "idle"
    mode: str | None = None
    requested_at: float | None = None
    fired_at: float | None = None
    targets: list = field(default_factory=list)
    interrupted: list = field(default_factory=list)
    message: str = ""


def _runners(snapshot, targets):
    out = []
    for repo, name in targets:
        for r in snapshot["repos"].get(repo, {}).get("runners", []):
            if r["name"] == name:
                out.append(r)
    return out


def _in_progress_on(snapshot, targets):
    names = {(repo, name) for repo, name in targets}
    found = []
    for repo, section in snapshot["repos"].items():
        runs = [run for pr in section.get("prs", []) for run in pr["runs"]] + section.get("deploys", [])
        for run in runs:
            if run.get("jobs_unknown"):
                # The job list for this run could not be retrieved. We cannot confirm
                # whether one of its jobs was executing on a target runner, but we must
                # not silently assume it wasn't -- that would leave a genuinely
                # interrupted run un-rerun and invisible. Record it as a possible
                # interruption.
                found.append({
                    "repo": repo,
                    "run_id": run["run_id"],
                    "job_id": None,
                    "uncertain": True,
                    "rerun": None,
                })
                continue
            for job in run.get("jobs", []):
                if job.get("status") == "in_progress" and (repo, job.get("runner_name")) in names:
                    found.append({
                        "repo": repo,
                        "run_id": run["run_id"],
                        "job_id": job["job_id"],
                        "rerun": None,
                    })
    return found


def request(state, mode, targets, snapshot, now):
    if mode not in ("idle", "now"):
        s = RestartState(**asdict(state))
        s.message = f"rejected unrecognized restart mode: {mode!r}"
        return s
    if state.state not in ("idle", "failed", "stalled"):
        return state
    s = RestartState(mode=mode, targets=list(targets), requested_at=now)
    s.state = "draining" if mode == "idle" else "arming"
    s.message = "waiting for runners to go idle" if mode == "idle" else "restarting now"
    return s


def force(state, snapshot, now):
    if state.state not in ("draining", "stalled"):
        return state
    return request(RestartState(), "now", state.targets, snapshot, now)


def step(state, snapshot, now):
    s = RestartState(**asdict(state))
    if s.state == "draining":
        if all(not r["busy"] for r in _runners(snapshot, s.targets)):
            s.state, s.fired_at, s.message = "restarting", now, "fired restart script"
            return s, [{"type": "fire"}]
        if now - s.requested_at > DRAIN_STALL_S:
            s.state, s.message = "stalled", "still busy after 20 minutes; force to restart now"
        return s, []
    if s.state == "arming":
        s.interrupted = _in_progress_on(snapshot, s.targets)
        s.state, s.fired_at, s.message = "restarting", now, "fired restart script"
        return s, [{"type": "fire"}]
    if s.state == "restarting":
        s.state, s.message = "verifying", "waiting for runners to come back online"
        return s, []
    if s.state == "verifying":
        runners = _runners(snapshot, s.targets)
        if runners and all(r["status"] == "online" for r in runners):
            actions = []
            seen = set()
            for item in s.interrupted:
                key = (item["repo"], item["run_id"])
                if key in seen or item["rerun"] is not None:
                    continue
                seen.add(key)
                actions.append({"type": "rerun", "repo": item["repo"], "run_id": item["run_id"]})
            for item in s.interrupted:
                if item["rerun"] is None:
                    item["rerun"] = "requested"
            s.state, s.message = "idle", f"restart complete; {len(actions)} run(s) re-run"
            return s, actions
        if now - s.fired_at > VERIFY_WINDOW_S:
            s.state, s.message = "failed", "runners did not come back online within 5 minutes"
        return s, []
    return s, []


def to_dict(state):
    return asdict(state)
