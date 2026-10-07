# Validation

## v0.1 local checks

Validated on macOS with Python 3.14. The declared minimum is Python 3.11; this
session did not run a separate Python-version or operating-system matrix.

| Check | Result |
| --- | --- |
| Synthetic adapter, verification, fleet, and CLI tests | Run with standard-library `unittest` |
| Editable package installation | Installed successfully in the repository's `.venv` |
| Installed `awk-kit` entry point | All three command groups available |
| Real history adapters | Two recent sessions from each of the six sources imported without adapter errors |
| Source-store safety | Read-only adapters; synthetic SQLite tests confirm source bytes remain unchanged |
| Worker launch integration | Preview and mocked execution tested |
| Real worker launch | Not performed; no production worker was started |
| Browser/Electron behavior | No bundled app harness; project-specific gates must supply it |

Runtime smoke-test data was written only to the private local state directory.
No real histories, runtime databases, worker reports, or credentials are included
in the repository.

## Reproduce

```bash
python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m agent_workflow_kit --help
git diff --check

# Requires an initial Git commit.
awk-kit verify run --project . --gates agent-gates.json
awk-kit verify status --project . --gates agent-gates.json
```

The verification receipt records the exact commit, working-tree/index hashes,
configuration digest, check outcomes, and timestamps. It lives outside Git and
is more authoritative than a static test-count claim in this document.
