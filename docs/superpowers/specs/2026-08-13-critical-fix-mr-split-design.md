# Critical Fix MR Split Design

## Goal

Reconstruct the complete fix history currently on `thermi/thermi` relative to
`main` into independent, reviewable branches and merge requests. Every branch
must start from `main`, contain only its thematic changes, and preserve the
behavior proven by the full suite (`12022 passed, 332 skipped`).

## Branches

1. `fix/tokenizer-model-routing`
   - Tokenizer registry, DeepSeek, provider-prefixed models, `/v1/compress`
     counting, and non-string tool-call coercion.
2. `fix/kompress-startup-health`
   - Kompress preload, deferred loading, health reconciliation, cache
     promotion, remote batching, and ONNX behavior.
3. `fix/ccr-cache-integrity`
   - CCR markers, retrieval injection, prefix lineage, cache TTL,
     tool-search, and streaming CCR behavior.
4. `fix/proxy-runtime-accounting`
   - Retry behavior, batch handlers, request outcomes, cost/history schemas,
     dashboard statistics, telemetry, and tracker reloads.
5. `fix/windows-install-lifecycle`
   - Windows hooks, RTK/Serena, SQLite cleanup, subprocess encoding, file
     modes, path portability, and install supervisors.
6. `fix/memory-storage-safety`
   - SQLite/vector/graph stores, memory initialization, malformed memory data,
     TrafficLearner bounds, and project isolation.
7. `test/compatibility-isolation`
   - Test-only fixes, optional dependency isolation, OpenCode configuration
     isolation, retired contract updates, and fixture cleanup.
8. `docs/critical-fix-tracking`
   - The critical GitHub tracking document.

## History Handling

The source branch contains 84 commits relative to the selected upstream base.
Commits are assigned by changed files and commit intent. If a commit spans
multiple themes, it is split by applying its file-level patch to the relevant
branch. If a later commit requires a prerequisite from another theme, the
smallest prerequisite is included and documented in the merge request.

No unrelated worktree changes are included: Docker environment files, local
`.serena/memories/`, `Microsoft/`, and unrelated planning documents remain
untouched.

## Validation

Each branch receives:

- The narrowest affected test group.
- Ruff and mypy through the repository hooks where applicable.
- A final comparison against `main` showing only the branch’s intended files.

The aggregate result is checked on the final reconstructed branch. Merge
requests are created only after their branch diff and test evidence are
reviewed.

## Merge Requests

Each MR will use a conventional title matching its branch, include the list of
source commits or split prerequisites, cite verified upstream GitHub PRs/issues
from `docs/critical-test-suite-fixes-github-tracker.md`, and state any
environment-specific skips.
