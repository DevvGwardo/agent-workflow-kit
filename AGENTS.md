# Agent Workflow Kit

This repository contains three agent-agnostic local tools. Keep dependencies
minimal and reuse worker launchers rather than building another agent runtime.

- Never commit transcripts, runtime databases, credentials, or real worker briefs.
- History source databases must be opened read-only.
- Verification commands use argument arrays, never implicit shell evaluation.
- GitHub writes, deployment, merging, cancellation, and cleanup are not automatic.
- Keep verification evidence bound to Git state and the gate configuration.
- Validate with `python3 -m unittest discover -s tests -v`.
- Check CLI behavior with `PYTHONPATH=src python3 -m agent_workflow_kit --help`.
