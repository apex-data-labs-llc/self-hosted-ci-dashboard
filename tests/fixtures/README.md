# Fixtures

Hand-authored, minimal synthetic fixtures using generic placeholder content (owner
`octocat`, repos `hello-world`/`spoon-knife`, runners `runner-1`/`runner-2`, generic job
and step names). They match the real GitHub API response shapes closely enough to
exercise `collector.py`'s parsing logic, but only carry the fields that logic actually
reads -- they are not full recordings of real API responses.

- `rate_limit.json` -- `GET /rate_limit` shape.
- `runners.json` -- `GET /repos/{owner}/{repo}/actions/runners` shape.
- `pulls.json` -- `GET /repos/{owner}/{repo}/pulls?state=open` shape (trimmed to the
  fields collector.py reads: `number`, `title`, `head.ref`, `head.sha`, `html_url`,
  `updated_at`).
- `runs.json` -- `GET /repos/{owner}/{repo}/actions/runs` shape.
- `jobs_ui.json` -- a completed, all-green job with a full step list, one of each
  visible category plus a couple of "setup"/"other" steps, used to check that every
  real step name lands in a real category.
- `jobs_failed.json` -- one job with a failing step per category-relevant area.
- `jobs_skipped.json` -- every job/step marked `completed`/`skipped`.
- `jobs_queued.json` -- queued jobs with `steps: []` (GitHub does not populate a job's
  steps until it starts).
- `local_probe_sample.txt` -- a sample local-process listing matching the
  `local_probe.pattern` regex documented in `config.example.json`.
