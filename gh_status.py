"""GitHub platform health from githubstatus.com (a public Statuspage feed).

Unauthenticated, and not part of the GitHub REST API, so polling it spends none of the
API quota the collector depends on. Standard library only.

Nothing here ever reports "operational" for something it did not actually read: a
component named in config but absent from the feed comes back with status None, and a
feed that is missing the fields we rely on raises instead of being read as healthy.
"""
import json
import urllib.request

STATUS_URL = "https://www.githubstatus.com/api/v2/summary.json"
PAGE_URL = "https://www.githubstatus.com"

# Components the dashboard's data depends on. Matched case-insensitively against the
# feed's component names; a name that is not in the feed is reported, not dropped.
DEFAULT_COMPONENTS = ["API Requests", "Actions", "Pull Requests", "Webhooks", "Git Operations"]

# A major outage of one of these makes a collector pass mostly error out, so the
# collector backs off while it lasts (see collector.next_interval).
CRITICAL_COMPONENTS = ("api requests", "actions")

# Which snapshot error sections each component can explain.
SECTION_COMPONENTS = {
    "runners": ("actions", "api requests"),
    "runs": ("actions", "api requests"),
    "jobs": ("actions", "api requests"),
    "prs": ("pull requests", "api requests"),
}


def fetch_summary(url=STATUS_URL, timeout=10):
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "ci-dashboard"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_summary(data, wanted):
    """Reduce a summary.json payload to what the dashboard shows. Raises ValueError if
    the fields it relies on are missing, so a changed feed reads as an error, never as
    an all-clear."""
    if not isinstance(data, dict):
        raise ValueError("status feed is not a JSON object")
    status = data.get("status")
    comps = data.get("components")
    if not isinstance(status, dict) or "indicator" not in status or not isinstance(comps, list):
        raise ValueError("status feed is missing 'status.indicator' or 'components'")
    by_name = {}
    for c in comps:
        if isinstance(c, dict) and c.get("name"):
            by_name.setdefault(c["name"].strip().lower(), c)
    components = []
    for name in wanted:
        c = by_name.get(name.strip().lower())
        components.append({"name": name, "status": c.get("status") if c else None,
                           "in_feed": c is not None})
    incidents = []
    for inc in data.get("incidents") or []:
        if not isinstance(inc, dict):
            continue
        incidents.append({"name": inc.get("name"), "status": inc.get("status"),
                          "impact": inc.get("impact"), "url": inc.get("shortlink")})
    return {
        "indicator": status.get("indicator"),
        "description": status.get("description"),
        "components": components,
        "incidents": incidents,
        "page_url": PAGE_URL,
    }


def _status_of(health, name):
    for c in (health or {}).get("components") or []:
        if c["name"].strip().lower() == name:
            return c.get("status")
    return None


def major_outage(health):
    """True only when the feed was actually read (not stale) and says a critical
    component is in major outage."""
    if not health or health.get("stale") or health.get("components") is None:
        return False
    return any(_status_of(health, n) == "major_outage" for n in CRITICAL_COMPONENTS)


def explain_errors(snapshot, health):
    """If a repo section failed and GitHub reports a matching component as not
    operational, return one line linking the two; else None."""
    if not health or health.get("stale"):
        return None
    failed = set()
    for section in ((snapshot or {}).get("repos") or {}).values():
        failed.update((section or {}).get("errors") or {})
    notes = []
    for key in sorted(failed):
        for comp in SECTION_COMPONENTS.get(key, ()):
            st = _status_of(health, comp)
            if st and st != "operational":
                notes.append(f"{key} errors: GitHub reports {comp.title()} as {st.replace('_', ' ')}")
                break
    return "; ".join(notes) or None
