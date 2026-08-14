# Critical Fix MR Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconstruct the complete fix history on `thermi/thermi` relative to `main` into eight independently reviewable Git branches and merge requests.

**Architecture:** Use `main` as the immutable base for every branch. Classify the existing commits by changed files and intent, cherry-pick complete commits where boundaries are clean, and split mixed commits by file-level patches only when necessary. Keep unrelated worktree changes out of every branch.

**Tech Stack:** Git, GitHub CLI/API, pytest, Ruff, mypy, Markdown.

## Global Constraints

- Every branch starts from local `main`, not from another fix branch.
- Preserve the existing committed fixes; do not rewrite or amend existing commits.
- Never stage `docker/.env2`, `docker/docker-compose.native.yml`, `.serena/memories/`, `Microsoft/`, or unrelated planning files.
- Use conventional branch names and MR titles.
- Run focused tests for each branch before creating its MR.
- Include verified upstream issue/PR links from `docs/critical-test-suite-fixes-github-tracker.md`.

### Task 1: Freeze the Source Inventory

**Files:**
- Read: `docs/superpowers/specs/2026-08-13-critical-fix-mr-split-design.md`
- Read: `docs/critical-test-suite-fixes-github-tracker.md`

- [ ] **Step 1: Confirm the source range**

Run:

```bash
git merge-base main HEAD
```

Expected: the merge base and complete source commit list are recorded before branch creation.

- [ ] **Step 2: Map commits to branch themes**

Use changed files and commit messages to assign each source commit to exactly one of:

```text
fix/tokenizer-model-routing
fix/kompress-startup-health
fix/ccr-cache-integrity
fix/proxy-runtime-accounting
fix/windows-install-lifecycle
fix/memory-storage-safety
```

Mixed commits must be listed with the exact files that will be applied to each branch.

### Task 2: Create Isolated Worktrees

**Files:**
- Create outside the primary checkout: one worktree per branch.

- [ ] **Step 1: Create worktrees from `main`**

For each branch:

```bash
git worktree add .worktrees/<branch-slug> -b <branch-name> main
```

Expected: each branch reports `main` as its base and starts clean.

- [ ] **Step 2: Verify isolation**

Run in every worktree:

```bash
git status --short --branch
git log -1 --oneline
```

Expected: no unrelated Docker, Serena, Microsoft, or planning files appear.

### Task 3: Build Each Thematic Branch

**Files:**
- Modify: only files belonging to the current branch theme.
- Preserve: all unrelated files and all unrelated source commits.

- [ ] **Step 1: Apply source commits**

Use complete cherry-picks for clean commits:

```bash
git cherry-pick <source-commit>
```

For a mixed commit, apply only the intended file patch:

```bash
git diff <commit>^ <commit> -- <file> | git apply --index
git commit -m "<conventional message>"
```

- [ ] **Step 2: Resolve dependencies explicitly**

If a cherry-pick fails because a prerequisite belongs to another theme, include the smallest prerequisite commit and record it in that branch’s MR body under `Cross-branch prerequisite`.

- [ ] **Step 3: Verify branch contents**

Run:

```bash
git diff --name-only main...HEAD
git log --oneline main..HEAD
```

Expected: only the branch’s intended files and commits are present.

- [ ] **Step 4: Commit branch assembly**

Use a conventional commit only for any split or dependency-resolution commit:

```bash
git add <intended-files>
```

### Task 4: Validate Branches

**Files:**
- Test: affected test modules selected from the source commits.

- [ ] **Step 1: Run focused tests**

Examples:

```bash
pytest tests/test_compress_route_tokenizer_by_model.py tests/test_tokenizers.py
pytest tests/test_proxy_warmup.py tests/test_kompress_preload_deferral.py
pytest tests/test_ccr.py tests/test_proxy_ccr.py tests/test_sse_thinking_blocks.py
pytest tests/test_proxy_retry_429.py tests/test_proxy_project_savings.py
pytest tests/test_rtk_installer.py tests/test_wrap_code_memory.py tests/test_sqlite_vector_index.py
pytest tests/test_memory_system.py tests/test_security_validations.py
pytest tests/test_proxy/test_interceptors_base.py tests/test_toin_full_integration.py
pytest docs/critical-test-suite-fixes-github-tracker.md
```

Use only the commands relevant to each branch; do not claim a branch is green without fresh output.

- [ ] **Step 2: Run static checks on touched Python files**

```bash
ruff check <touched-python-files>
ruff format --check <touched-python-files>
```

- [ ] **Step 3: Record validation**

Add the exact command and result to the MR body. Mention platform-specific skips and external-model requirements.

### Task 5: Push Branches and Create MRs

**Files:**
- No source changes.
- MR metadata only.

- [ ] **Step 1: Push each branch**

```bash
git push -u thermi <branch-name>
```

- [ ] **Step 2: Create each MR**

Use `gh pr create` against the repository’s configured remote and base `main`:

```bash
gh pr create --base main --head <branch-name> \
  --title "<conventional MR title>" \
  --body-file <mr-body-file>
```

Each body must include:

```text
Summary
Source commits
Cross-branch prerequisites
Verified upstream issues/PRs
Focused test commands and results
Full-suite evidence or remaining scope
```

- [ ] **Step 3: Verify MR URLs and branch state**

Run:

```bash
gh pr view <number> --json number,title,state,url,headRefName,baseRefName
git status --short --branch
```

Expected: each MR is open against `main`, each head branch is pushed, and the primary checkout’s unrelated changes remain unstaged.
