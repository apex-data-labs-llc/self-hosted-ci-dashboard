import base64
import subprocess
import sys

import pytest

import gh_client as G


def test_classify_error():
    assert G.classify_error("HTTP 403: API rate limit exceeded for user") == "rate_limit"
    assert G.classify_error("gh: To get started with GitHub CLI, please run: gh auth login") == "auth"
    assert G.classify_error("HTTP 401: Bad credentials") == "auth"
    assert G.classify_error("", timed_out=True) == "timeout"
    assert G.classify_error("boom") == "other"


def _run_via_python(monkeypatch, payload_bytes):
    """Point gh_client at `python -c <script>` instead of a real `gh`, so the real
    subprocess.run(..., text=True, ...) decode path in gh_client._run runs against
    exact, deliberately chosen bytes. The payload is shipped base64-encoded on the
    command line and decoded inside the child so no raw special/invalid bytes ever have
    to survive argv/shell quoting -- only the child's stdout carries them, which is the
    thing under test.
    """
    b64 = base64.b64encode(payload_bytes).decode("ascii")
    script = f"import sys,base64; sys.stdout.buffer.write(base64.b64decode({b64!r}))"
    monkeypatch.setattr(G, "resolve_gh", lambda: sys.executable)
    return G.gh_json(["-c", script])


def test_run_decodes_utf8_and_replaces_invalid_byte(monkeypatch):
    """Regression test: real `gh` output contains both genuine multi-byte UTF-8 (real
    step names can carry non-ASCII characters, e.g. a section sign) and, in the wild, a
    lone invalid byte that is illegal both in a legacy codepage and as a standalone
    UTF-8 continuation byte. Before the fix this raised UnicodeDecodeError and crashed
    the calling jobs fetch outright. It must not raise, the genuine UTF-8 character must
    survive intact, and the invalid byte must degrade visibly (U+FFFD, the standard
    Unicode replacement character) rather than silently vanishing or crashing the pass.
    """
    section_sign = "§"  # SECTION SIGN, i.e. b"\xc2\xa7" in UTF-8
    replacement_char = "�"  # REPLACEMENT CHARACTER
    section_sign_utf8 = section_sign.encode("utf-8")
    payload = (
        b'{"note": "release notes (' + section_sign_utf8 + b' 10.4)", '
        b'"bad": "before\x9dafter"}'
    )

    result = _run_via_python(monkeypatch, payload)

    assert result["note"] == f"release notes ({section_sign} 10.4)"
    assert result["bad"] == f"before{replacement_char}after"


def test_run_raises_on_none_stdout_instead_of_returning_it(monkeypatch):
    """Regression test. Observed in the wild: a call returned a `None` stdout from
    subprocess.run despite returncode == 0, and gh_paginate's `out.splitlines()`
    crashed on it with an unrelated-looking AttributeError ("'NoneType' object has no
    attribute 'splitlines'") that hid the real failure inside errors['jobs']. A None
    here has no honest reading as "empty output" (that's "", and is handled separately
    and correctly) -- it means the call did not genuinely produce a response, which is
    a failed call. _run must raise a GhError with a real, attributable message instead
    of returning None for a caller to crash on later.
    """
    fake_completed = subprocess.CompletedProcess(args=["gh", "api", "rate_limit"],
                                                  returncode=0, stdout=None, stderr="")
    monkeypatch.setattr(G, "resolve_gh", lambda: "gh")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake_completed)

    with pytest.raises(G.GhError) as exc_info:
        G._run(["api", "rate_limit"], timeout=20)

    assert "no output" in str(exc_info.value)


def test_classify_error_separates_secondary_rate_limit():
    assert G.classify_error("HTTP 403: You have exceeded a secondary rate limit") == "secondary_rate_limit"
    assert G.classify_error("HTTP 403: API rate limit exceeded for user") == "rate_limit"
