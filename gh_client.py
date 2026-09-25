"""The only module that shells out to gh."""
import json
import re
import shutil
import subprocess

# resolve_gh() used to call shutil.which() -- a fresh filesystem/PATH scan -- on every
# single `gh` invocation. A pass makes on the order of 100+ calls, so that overhead
# accumulates measurably. PATH does not change mid-process, so resolve once and cache: a
# plain module-level variable is enough (CPython's GIL makes the read/write here atomic;
# the worst a race can do is redundant identical work, never a torn/partial value).
_GH_PATH = None


def resolve_gh():
    """Resolve the `gh` executable through PATH the way a shell would, once.

    Windows' CreateProcess (which subprocess.run reaches with shell=False) appends only
    `.exe` when given an extension-less name -- it never tries PATHEXT's other entries
    (.CMD, .BAT, ...), so a `gh.cmd` shim earlier on PATH is silently skipped in favor of
    any real gh.exe found elsewhere, no matter the PATH order. shutil.which() implements
    the real PATHEXT search (as cmd.exe and POSIX shells do), so resolve through it and
    hand subprocess an explicit path. Falls back to the literal name if not found, so the
    normal "gh not installed" error still surfaces on all platforms.
    """
    global _GH_PATH
    if _GH_PATH is None:
        _GH_PATH = shutil.which("gh") or "gh"
    return _GH_PATH


class GhError(RuntimeError):
    def __init__(self, message, kind):
        super().__init__(message)
        self.kind = kind


def classify_error(stderr, timed_out=False):
    if timed_out:
        return "timeout"
    s = stderr or ""
    # Checked first: GitHub's secondary (abuse) limit message also contains "rate
    # limit", but it needs a longer back-off rather than waiting for the hourly reset.
    if re.search(r"secondary rate limit", s, re.I):
        return "secondary_rate_limit"
    if re.search(r"rate limit", s, re.I):
        return "rate_limit"
    if re.search(r"auth login|bad credentials|HTTP 401|not logged in", s, re.I):
        return "auth"
    return "other"


_CMD_META = "&|<>^()"


def _caret_escape_for_cmd(s):
    """Make one argument immune to cmd.exe's command-line grammar.

    Only used when the resolved `gh` is a .cmd/.bat (real gh.exe is a normal PE image
    CreateProcess launches directly -- cmd.exe never sees its argv, so real usage is
    unaffected). A real `gh api` call routinely carries a query string like
    `pulls?state=open&per_page=100`. Whether that argument reaches cmd.exe via
    CreateProcess's automatic COMSPEC fallback for batch files, or via an explicit
    `cmd /c`, cmd.exe re-parses the *entire* resulting command line with its own grammar,
    where `&` separates commands. Wrapping the argument in double quotes does not
    reliably save it: CreateProcess's batch-file fallback ALSO wraps the whole original
    command line in one more layer of quotes, and the two quoted regions' parity can
    cancel out, un-quoting the inner argument again (a quoted
    `pulls?state=open&per_page=100` can still get split, running `per_page=100` as its
    own command and failing with "'per_page' is not recognized"). Caret-escaping each
    cmd.exe metacharacter individually (`&` -> `^&`) sidesteps the quote-parity problem
    entirely: it is quote nesting independent, so it survives being re-wrapped by the
    fallback.
    """
    return "".join(("^" + c) if c in _CMD_META else c for c in s)


def _run(args, timeout):
    gh = resolve_gh()
    if gh.lower().endswith((".cmd", ".bat")):
        argv = [gh, *(_caret_escape_for_cmd(a) for a in args)]
    else:
        argv = [gh, *args]
    try:
        # text=True with no explicit encoding decodes the child's bytes using
        # locale.getpreferredencoding() -- on many Windows machines that is a legacy
        # codepage, not UTF-8. Real `gh` output is UTF-8 (repo content and step names are
        # not guaranteed ASCII-only, and real API payloads can carry arbitrary UTF-8
        # bytes that are flatly illegal in a legacy codepage). Decoding with the wrong
        # codec on such a byte raises UnicodeDecodeError and can crash a jobs fetch
        # outright. encoding="utf-8" decodes correctly; errors="replace" means one
        # genuinely malformed byte degrades to a single U+FFFD in that one field instead
        # of destroying the entire response (errors="strict", the default, crashes on
        # it; errors="ignore" would silently delete bytes and hide real corruption --
        # "replace" is the only choice that keeps a decode problem visible in the data
        # while not sinking the pass).
        result = subprocess.run(argv, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GhError(f"gh timed out after {timeout}s: {' '.join(args[:3])}", "timeout")
    if result.returncode != 0:
        err = (result.stderr or "").strip() or "gh command failed"
        raise GhError(err, classify_error(err))
    if result.stdout is None:
        # subprocess.run(capture_output=True, text=True, ...) is documented to always
        # hand back a str (possibly empty) on a completed call; a genuinely empty
        # response from `gh` is "" and correctly means zero results downstream, which is
        # NOT the same thing as this call having failed to produce a response at all. A
        # None here has no honest interpretation as "empty" -- collapsing it to "" would
        # be exactly the kind of fabrication (silently reporting "nothing to see here"
        # for "we don't actually know") this tool exists to avoid. Treat it as the
        # failed call it is and say so, with the real returncode attached.
        raise GhError(f"gh produced no output (returncode={result.returncode})", "other")
    return result.stdout


def gh_text(args, timeout=20):
    """Raw stdout of one `gh` call (e.g. a job log), with the same Windows argv
    escaping, UTF-8 decoding and error classification as gh_json."""
    return _run(args, timeout)


def gh_json(args, timeout=20):
    out = _run(args, timeout)
    return json.loads(out) if out.strip() else None


def gh_paginate(path, jq, timeout=60):
    """jq must produce a JSON array per page; pages are concatenated."""
    out = _run(["api", "--paginate", path, "--jq", jq], timeout)
    items = []
    for line in out.splitlines():
        line = line.strip()
        if line:
            items.extend(json.loads(line))
    return items
