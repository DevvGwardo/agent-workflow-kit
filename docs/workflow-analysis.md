# Workflow analysis

**Assessment date:** October 6, 2026

**Method:** Sampled 265 session files/stores, recent Hermes and OpenCode messages,
local workflow documentation, and worker reports. This was a representative
investigation, not an exhaustive audit of every historical conversation.

## Outcome

The most valuable improvements are:

1. Cross-agent session search and recovery.
2. Executable, commit-bound verification gates.
3. Durable, worktree-aware coordination.

The recurring problem is carrying trustworthy state from **session → worker →
verified change → release**, not a shortage of coding agents.

## 1. Cross-agent session search and recovery

### Observations

- Work moves among Claude Code, Factory Droid, Codex, Cursor, Hermes, and OpenCode.
- Sessions ask about work happening in another session, previous changes,
  interrupted workers, and whether pull requests were opened.
- The existing ATS index's newest dated conversations were August 2, while
  October sessions were actively being written.
- A second overlapping history index reached September 30.
- Existing ATS adapters covered four agents, not Droid and OpenCode.

### Recommendation

Maintain one automatically refreshed local retrieval surface with explicit
freshness, source references, and bounded recovery context. Separate actual
conversation text from tool payloads and injected instructions.

### Implementation in this repository

`awk-kit history` adds six native source adapters, incremental indexing,
FTS search, source-linked recovery, bounded watch mode, and freshness statistics.
It leaves previous indexes and raw histories untouched.

## 2. Executable verification gates

### Observations

- Requests repeatedly ask whether changes actually work.
- A recent desktop build reached a blank Electron app.
- Worker reports frequently distinguish passing local tests from untested
  packaged-app or live-integration behavior.
- Follow-ups distinguish web deployment from shipping a desktop release.
- Existing smoke harnesses and release checks are useful, but evidence is spread
  across gate files, reports, and session summaries.

Scoped workers are not wrong to leave integration checks to the coordinator.
The missing piece is a clear, durable integration boundary.

### Recommendation

Bind evidence to the precise commit, working-tree contents, gate configuration,
time, and artifacts. Distinguish implementation, integration, and release.
Make failures and skipped checks visible.

### Implementation in this repository

`awk-kit verify` runs reviewed argument-array commands, saves private receipts,
and invalidates stale evidence. Project-specific browser/Electron/release checks
remain in their existing harnesses.

## 3. Worktree-aware coordination

### Observations

- Explicit parallel work and external workers are common.
- Recent workers needed corrections to avoid editing shared test files.
- An interrupted worker needed its task and constraints restored.
- Many journal “projects” were worktrees of one repository, not distinct products.
- The machine already has capable worker launchers.

### Recommendation

Improve coordination rather than introduce another agent runtime. Track durable
briefs, file ownership, blockers, worktree commits, and evidence-backed completion.
Warn about overlaps before starting work.

### Implementation in this repository

`awk-kit fleet` provides an atomic ownership registry, repository deduplication,
private task briefs, heartbeat/blocker events, launch previews, and fresh-evidence
completion checks. Actual execution delegates to `warroom` or `tmux-orch`.

## Priority and adoption

Start with history freshness, then project acceptance gates, then register
parallel work through the fleet layer.

Keep environment discovery lightweight. Tools such as `devctx` remain useful,
but another dashboard or model bridge would not address the main bottleneck.

## Privacy

This document contains the conclusions, not raw session excerpts, customer
identifiers, credentials, or private runtime data. Nothing here requires
publishing the indexed histories.
