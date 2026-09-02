import restart as R


def _snap(busy_by_runner, running_jobs=(), online=True, unknown_runs=()):
    runners = [{"name": n, "status": "online" if online else "offline", "busy": b, "current_job": None}
               for n, b in busy_by_runner.items()]
    runs = [{"run_id": rid, "jobs": [{"job_id": jid, "status": "in_progress", "runner_name": rn}]}
            for rid, jid, rn in running_jobs]
    runs += [{"run_id": rid, "jobs_unknown": True} for rid in unknown_runs]
    prs = [{"runs": runs}]
    return {"repos": {"hello-world": {"runners": runners, "prs": prs, "deploys": []}}}


T = [("hello-world", "runner-1"), ("hello-world", "runner-2")]


def test_idle_mode_waits_until_all_targets_idle_then_fires():
    s = R.request(R.RestartState(), "idle", T, _snap({"runner-1": True, "runner-2": False}), now=0)
    assert s.state == "draining"
    s, actions = R.step(s, _snap({"runner-1": True, "runner-2": False}), now=60)
    assert actions == [] and s.state == "draining"
    s, actions = R.step(s, _snap({"runner-1": False, "runner-2": False}), now=120)
    assert actions == [{"type": "fire"}] and s.state == "restarting" and s.interrupted == []


def test_idle_mode_stalls_after_20_minutes():
    s = R.request(R.RestartState(), "idle", T, _snap({"runner-1": True, "runner-2": True}), now=0)
    s, actions = R.step(s, _snap({"runner-1": True, "runner-2": True}), now=R.DRAIN_STALL_S + 1)
    assert s.state == "stalled" and actions == [] and "20" in s.message


def test_force_from_stalled_fires_now_and_captures_interrupted():
    s = R.request(R.RestartState(), "idle", T, _snap({"runner-1": True, "runner-2": True}), now=0)
    s, _ = R.step(s, _snap({"runner-1": True, "runner-2": True}), now=R.DRAIN_STALL_S + 1)
    snap = _snap({"runner-1": True, "runner-2": True}, running_jobs=[(10, 100, "runner-1"), (11, 101, "runner-2")])
    s = R.force(s, snap, now=2000)
    s, actions = R.step(s, snap, now=2000)
    assert actions == [{"type": "fire"}] and s.mode == "now"
    assert sorted(i["run_id"] for i in s.interrupted) == [10, 11]


def test_now_mode_reruns_each_interrupted_run_once_after_runners_online():
    snap = _snap({"runner-1": True, "runner-2": False}, running_jobs=[(10, 100, "runner-1")])
    s = R.request(R.RestartState(), "now", T, snap, now=0)
    s, actions = R.step(s, snap, now=0)
    assert actions == [{"type": "fire"}] and s.state == "restarting"
    s, actions = R.step(s, _snap({"runner-1": False, "runner-2": False}, online=False), now=30)
    assert actions == [] and s.state == "verifying"
    s, actions = R.step(s, _snap({"runner-1": False, "runner-2": False}, online=True), now=60)
    assert actions == [{"type": "rerun", "repo": "hello-world", "run_id": 10}]
    assert s.state == "idle" and s.interrupted[0]["rerun"] == "requested"
    s, actions = R.step(s, _snap({"runner-1": False, "runner-2": False}), now=90)
    assert actions == []


def test_now_mode_never_reruns_unrelated_failures():
    snap = _snap({"runner-1": False, "runner-2": False})
    s = R.request(R.RestartState(), "now", T, snap, now=0)
    s, _ = R.step(s, snap, now=0)
    s, _ = R.step(s, _snap({"runner-1": False, "runner-2": False}, online=True), now=30)
    s, actions = R.step(s, _snap({"runner-1": False, "runner-2": False}, online=True), now=60)
    assert actions == [] and s.state == "idle"


def test_verify_times_out_to_failed():
    snap = _snap({"runner-1": False, "runner-2": False})
    s = R.request(R.RestartState(), "now", T, snap, now=0)
    s, _ = R.step(s, snap, now=0)
    s, _ = R.step(s, _snap({"runner-1": False, "runner-2": False}, online=False), now=30)
    s, actions = R.step(s, _snap({"runner-1": False, "runner-2": False}, online=False), now=30 + R.VERIFY_WINDOW_S + 1)
    assert s.state == "failed" and actions == [] and "online" in s.message


def test_to_dict_is_json_safe():
    import json
    json.dumps(R.to_dict(R.RestartState()))


def test_arming_with_jobs_unknown_marks_run_as_uncertain_interruption():
    # A run whose job list could not be retrieved must not be silently treated as "not
    # interrupted" -- we genuinely don't know, so it is recorded as a possible
    # interruption with an uncertain marker.
    snap = _snap({"runner-1": True, "runner-2": True}, unknown_runs=[42])
    s = R.request(R.RestartState(), "now", T, snap, now=0)
    s, actions = R.step(s, snap, now=0)
    assert actions == [{"type": "fire"}] and s.state == "restarting"
    assert len(s.interrupted) == 1
    item = s.interrupted[0]
    assert item["run_id"] == 42
    assert item["job_id"] is None
    assert item["uncertain"] is True
    assert item["rerun"] is None


def test_jobs_unknown_run_is_rerun_during_verify():
    snap = _snap({"runner-1": True, "runner-2": True}, unknown_runs=[42])
    s = R.request(R.RestartState(), "now", T, snap, now=0)
    s, _ = R.step(s, snap, now=0)
    s, _ = R.step(s, _snap({"runner-1": False, "runner-2": False}, online=False), now=30)
    s, actions = R.step(s, _snap({"runner-1": False, "runner-2": False}, online=True), now=60)
    assert actions == [{"type": "rerun", "repo": "hello-world", "run_id": 42}]
    assert s.interrupted[0]["rerun"] == "requested"


def test_request_rejects_unrecognized_mode_leaving_state_unchanged():
    for bad_mode in ("bogus", "", None):
        original = R.RestartState()
        s = R.request(original, bad_mode, T, _snap({"runner-1": False, "runner-2": False}), now=0)
        assert s.state == "idle"
        assert s.mode is None
        assert s.targets == []
        assert s.requested_at is None
        assert repr(bad_mode) in s.message
        assert "rejected" in s.message


def test_request_rejects_unrecognized_mode_without_disturbing_in_progress_restart():
    snap = _snap({"runner-1": True, "runner-2": False})
    in_progress = R.request(R.RestartState(), "idle", T, snap, now=0)
    assert in_progress.state == "draining"
    rejected = R.request(in_progress, "bogus", T, snap, now=5)
    assert rejected.state == "draining"
    assert rejected.mode == "idle"
    assert rejected.targets == T
    assert "rejected" in rejected.message
    assert "bogus" in rejected.message


def test_request_accepts_idle_and_now_modes():
    snap = _snap({"runner-1": False, "runner-2": False})
    s_idle = R.request(R.RestartState(), "idle", T, snap, now=0)
    assert s_idle.state == "draining" and s_idle.mode == "idle"
    s_now = R.request(R.RestartState(), "now", T, snap, now=0)
    assert s_now.state == "arming" and s_now.mode == "now"


def test_matrix_job_same_run_id_two_target_runners_reruns_once():
    # A matrix job produces two jobs on the same run, on two different target runners.
    # Only one rerun action should ever be emitted for that run id -- emitting two
    # would restart the same CI work twice.
    snap = _snap(
        {"runner-1": True, "runner-2": True},
        running_jobs=[(10, 100, "runner-1")],
    )
    snap["repos"]["hello-world"]["prs"][0]["runs"][0]["jobs"].append(
        {"job_id": 101, "status": "in_progress", "runner_name": "runner-2"}
    )
    s = R.request(R.RestartState(), "now", T, snap, now=0)
    s, actions = R.step(s, snap, now=0)
    assert actions == [{"type": "fire"}] and s.state == "restarting"
    assert sorted(i["job_id"] for i in s.interrupted) == [100, 101]
    s, _ = R.step(s, _snap({"runner-1": False, "runner-2": False}, online=False), now=30)
    s, actions = R.step(s, _snap({"runner-1": False, "runner-2": False}, online=True), now=60)
    assert actions == [{"type": "rerun", "repo": "hello-world", "run_id": 10}]
    assert all(i["rerun"] == "requested" for i in s.interrupted if i["run_id"] == 10)
