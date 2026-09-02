import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    path = FIXTURES / name
    text = path.read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


@pytest.fixture(autouse=True)
def _collector_config():
    """collector.py's OWNER/REPOS/RUNNER_LOCAL_DIRS/DEPLOY_WORKFLOW_PATHS/PASS_BUDGET_S/
    JOBS_POOL_WORKERS carry no built-in values -- server.py sets them from config.json
    at startup. Tests set the same generic, synthetic values directly on the module so
    every test sees a consistent, private-data-free configuration regardless of import
    order, and each test gets a clean slate (a test that mutates these, e.g. the pass-
    budget tests, cannot leak into the next test).
    """
    import collector as C
    C.OWNER = "octocat"
    C.REPOS = ["hello-world", "spoon-knife"]
    C.RUNNER_LOCAL_DIRS = {
        ("hello-world", "runner-1"): "runner-1",
        ("hello-world", "runner-2"): "runner-2",
    }
    C.DEPLOY_WORKFLOW_PATHS = {
        "hello-world": {".github/workflows/deploy.yml", ".github/workflows/rollback.yml"},
        "spoon-knife": {".github/workflows/ci.yml"},
    }
    C.PASS_BUDGET_S = 150
    C.JOBS_POOL_WORKERS = 8
    yield
