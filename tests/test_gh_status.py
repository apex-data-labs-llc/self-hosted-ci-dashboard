import pytest

import gh_status as S
from tests.conftest import FIXTURES, load


def test_parse_summary_maps_wanted_components_and_incidents():
    h = S.parse_summary(load("gh_status_summary.json"), ["Actions", "api requests", "Codespaces"])
    assert h["indicator"] == "minor"
    by = {c["name"]: c for c in h["components"]}
    assert by["Actions"]["status"] == "partial_outage"
    assert by["api requests"]["status"] == "operational"  # case-insensitive match
    # A configured name missing from the feed is reported as unread, never operational.
    assert by["Codespaces"] == {"name": "Codespaces", "status": None, "in_feed": False}
    assert h["incidents"][0]["name"] == "Delayed Actions runs"
    assert h["incidents"][0]["url"] == "https://stspg.io/example"


@pytest.mark.parametrize("data", [None, [], {}, {"status": {}}, {"status": {"indicator": "none"}},
                                  {"status": {"indicator": "none"}, "components": {}}])
def test_parse_summary_rejects_a_feed_missing_required_fields(data):
    with pytest.raises(ValueError):
        S.parse_summary(data, S.DEFAULT_COMPONENTS)


def test_fetch_summary_reads_a_url():
    data = S.fetch_summary((FIXTURES / "gh_status_summary.json").as_uri())
    assert data["status"]["indicator"] == "minor"


def _health(**statuses):
    return {"stale": False, "components": [{"name": n, "status": st, "in_feed": True} for n, st in statuses.items()]}


def test_major_outage_only_for_critical_components_and_fresh_data():
    assert S.major_outage(_health(Actions="major_outage")) is True
    assert S.major_outage(_health(**{"API Requests": "major_outage"})) is True
    assert S.major_outage(_health(Actions="partial_outage")) is False
    assert S.major_outage(_health(Webhooks="major_outage")) is False
    stale = {**_health(Actions="major_outage"), "stale": True}
    assert S.major_outage(stale) is False
    assert S.major_outage({"components": None}) is False
    assert S.major_outage(None) is False


def test_explain_errors_links_failed_sections_to_degraded_components():
    snap = {"repos": {"hello-world": {"errors": {"jobs": ["HTTP 502"], "prs": "boom"}}}}
    note = S.explain_errors(snap, _health(Actions="partial_outage", **{"Pull Requests": "operational",
                                                                       "API Requests": "operational"}))
    assert "jobs errors" in note and "Actions" in note and "partial outage" in note
    assert "prs" not in note  # Pull Requests and API Requests are operational
    assert S.explain_errors(snap, {**_health(Actions="major_outage"), "stale": True}) is None
    assert S.explain_errors({"repos": {"hello-world": {"errors": {}}}}, _health(Actions="major_outage")) is None
