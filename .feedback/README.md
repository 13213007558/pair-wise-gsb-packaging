# Feedback task starting environment

Upstream: https://github.com/pypa/packaging
Upstream commit: `843887fd431f8b3c10f1b39e1d4066ba3516147c`
Prepared on macOS with CPython 3.12.11. No external database, queue, or cloud account is required.
The dependency file pins the installed runtime and test packages for the selected baseline. It is not a claim that all optional upstream test suites are installed.

From the repository root:

```sh
export NO_INTERNET=1 TZ=UTC
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r .feedback/requirements.txt -e .
.venv/bin/python -m pytest -q -o addopts= tests/test_metadata.py tests/test_requirements.py tests/test_markers.py tests/test_specifiers.py tests/test_direct_url.py tests/test_pylock.py tests/test_pylock_select.py -m 'not property'
```

Scope note: The scope is the command below, not all optional upstream integrations.

Observed baseline (2026-11-10): `10309 passed, 3 skipped in 4.41s`
Whole-command wall time: 4.887 seconds, exit code 0. This is one local measurement, not a runtime guarantee. The first install is excluded.

This seed adds preparation metadata only. Application sources and upstream tests are unchanged; there is no task solution in this commit.
