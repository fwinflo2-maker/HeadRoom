# Task 4 Report: Global Durable Pi/OMP Init and Shared Runtime

## RED

- Added focused native-init coverage first.
- Command: `.venv/bin/python -m pytest tests/test_cli/test_init_cli.py -k 'pi or omp or native' -v`
- Result: 3 failed, 8 passed. Failures proved Pi/OMP global detection, explicit Pi command/scope rejection, and `ArtifactRecord`/native runtime wiring were absent.

## GREEN

Implemented global-only `headroom init -g pi|omp` and bare global detection. Native targets use the shared `init-user` manifest, the existing persistent-task supervisor, one shared extension config artifact, and per-host package artifacts. The exact current Headroom release is validated before manifest/package/config/task mutation. OMP init imports no wrapper runtime and does not touch `models.yml`.

Manifest ownership saves are reloaded and checked for exact target/artifact persistence. Repeated initialization upserts artifact records by `(kind, path)`.

## Transactional rollback evidence

Parameterized tests cover task, config, and package failures. They assert restoration of the prior supervisor kind, targets, artifacts, task removal, and config cleanup after package failure. A separate test verifies initiating and rollback failures are combined into one Click error. Runtime code snapshots the prior manifest, host package state, extension config bytes/mode, supervisor kind/artifacts, and task files; rollback removes/restores package/config/task state and persists the prior manifest with reload verification.

## Validation

- `.venv/bin/python -m pytest tests/test_cli/test_init_cli.py tests/test_install/test_supervisors.py -q` — 116 passed.
- `.venv/bin/python -m pytest tests/test_cli/test_init_cli.py -k 'pi or omp or native' -q` — 21 passed, 65 deselected.
- `.venv/bin/ruff format --check headroom/cli/init.py tests/test_cli/test_init_cli.py tests/test_install/test_supervisors.py` — 3 files already formatted.
- `.venv/bin/ruff check headroom/cli/init.py tests/test_cli/test_init_cli.py tests/test_install/test_supervisors.py` — all checks passed.
- `.venv/bin/mypy headroom/cli/init.py` — success, no issues.
- `git diff --check` — passed before commit.

## Files and commit

- `headroom/cli/init.py`
- `tests/test_cli/test_init_cli.py`
- `tests/test_install/test_supervisors.py`
- Commit: `ca8e303d feat: add durable global Pi and OMP init`

## Self-review

- Scope limited to the three authorized task files in the commit.
- Reused the existing supervisor and Task 2/3 package/config lifecycle functions; no second manager, subprocess hook, wrapper runtime, model config, or runtime dependency added.
- Local native scope validation precedes release resolution and manifest creation.
- Manifest target/artifact persistence is verified by reload.

## Risks

- Rollback is best-effort across external host CLIs and OS supervisors; any rollback failure is surfaced together with the initiating failure rather than hidden.
- Config lifecycle race recovery behavior remains delegated to the reviewed Task 3 implementation and may retain its named recovery path.

## Unrelated unstaged confirmation

Unrelated pre-existing modified/untracked files remain unstaged and were not included in `ca8e303d`. After commit, `git diff --cached --name-only` is empty.

## Fix Round 1

### RED

Added focused regressions for the five review findings and ran:

- `.venv/bin/python -m pytest tests/test_cli/test_init_cli.py -k 'native or pi or omp' -q --tb=short`
- Result: 5 failed, 20 passed. Failures reproduced planner loss of native targets, manifest mutation before missing-binary failure, corrupt-manifest overwrite, config publish-then-fail leakage, and failure to preserve/combine a concurrent config change.

### Corrected transaction boundary

Native init now resolves every requested host binary and the exact extension release, then snapshots raw manifest bytes plus parsed ownership state before any runtime stop/save/install. Corrupt ownership state aborts with an actionable error and unchanged bytes. The runtime manifest is built only after preflight and explicitly restores the merged target list after planner normalization.

The transaction snapshots config bytes/mode, task files, package state, and the prior manifest before mutation. Failure restores package, config, supervisor/task files, and exact raw manifest bytes (or removes a newly created profile). Config rollback derives the exact managed bytes from the raw snapshot before calling the lifecycle helper, so publish-then-raise is recoverable even without an artifact return; differing concurrent bytes are preserved and reported alongside the initiating failure.

### Test isolation and GREEN

An autouse test fixture redirects extension config plus profile/manifest/Unix/Windows task helpers to `tmp_path` and verifies a user-state sentinel remains untouched.

- Full init/supervisor suite: 120 passed.
- Focused native suite: 25 passed, 65 deselected.
- Ruff format/check: passed.
- mypy `headroom/cli/init.py`: passed.
- `git diff --check`: passed.

## Fix Round 2

### RED

Added batch-transaction regressions and ran `.venv/bin/python -m pytest tests/test_cli/test_init_cli.py -k 'native_batch or native_crontab or native_runtime_state' -q --tb=short`.

Result: 5 failed. The failures demonstrated the missing batch helper, missing exact scheduler snapshot/restore, duplicate per-host shared work, and absent runtime-state restoration.

### Corrected batch transaction

Native targets are now preflighted together and passed to one `_init_native_hosts` transaction. The batch snapshots every host package state, raw manifest/config/task files, platform scheduler state, and prior runtime readiness before mutation. Shared config, persistent task registration, and runtime activation run once per batch. The manifest is not persisted with native claims until every package ensure succeeds; final targets/artifacts are saved and reload-verified together.

If any host fails, every earlier successful host is restored to its pre-state. The failing host is re-inspected to verify Task 2's internal mutation rollback. Rollback restores exact Linux crontab text including whitespace, macOS plist bytes/existence and loaded state, Windows task XML registrations/existence, task files, raw manifest bytes, shared config, and prior runtime running/stopped state. Initiating and rollback/verification failures remain combined.

### GREEN

- Full init/supervisor suite: 128 passed.
- Focused native suite: 33 passed, 65 deselected.
- Ruff format/check: passed.
- mypy `headroom/cli/init.py`: passed.
- `git diff --check`: passed.

## Fix Round 3

### RED

Added fail-closed scheduler, process-state, and provisional-startup regressions. Focused RED command:

- `.venv/bin/python -m pytest tests/test_cli/test_init_cli.py -k 'scheduler_snapshot_transient or crontab_snapshot or running_but_unready or restart_failure or strict_startup' -q --tb=short`
- Result: 6 failed, 1 passed. Failures reproduced transient scheduler queries treated as absence, readiness-only runtime snapshots, missing strict startup, and premature final claims.

### Corrected transaction narrative

Scheduler preflight now captures raw bytes. Linux recognizes absence only from `crontab -l` return code 1 with the authoritative `no crontab for` diagnostic; macOS recognizes unloaded only from launchctl 113/`Could not find service`; Windows recognizes task absence only from return code 1 plus `cannot find the file specified`. Every other query failure aborts before mutation. Rollback feeds exact crontab bytes back on stdin, restores exact plist bytes and loaded state, and restores exact exported task XML bytes and registration existence.

Runtime preflight records both `runtime_status` and readiness. Rollback stops transaction runtime state, restores prior stopped/running process state, verifies process status, and requires readiness again when the prior runtime was ready. Running-but-unready remains running; restoration failures are combined with the initiating failure.

After package/config success, native init creates a provisional manifest containing only pre-existing targets/artifacts, installs the shared task, persists that provisional ownership, and starts with a strict readiness helper. Only successful readiness permits the final manifest to claim native targets plus package/config/task artifacts. Strict startup failure rolls back packages, config, scheduler, manifest, and runtime with no final native claims.

### GREEN

- Full init/supervisor suite: 134 passed.
- Focused native suite: 39 passed, 65 deselected.
- Ruff format/check: passed.
- mypy `headroom/cli/init.py`: passed.
- `git diff --check`: passed.

## Fix Round 4

### RED

Added focused regressions for authoritative scheduler absence, failed absence rollback, raw crontab bytes, delayed runtime transitions, provisional-save ordering, and concurrent start-lock denial. The focused RED run selected 9 tests and all 9 failed against `a741dfaf`.

### Corrections

Scheduler classification now requires the documented return code and diagnostic together. Linux shared supervisor installation fails closed on unknown `crontab -l` failures and preserves raw crontab bytes. Linux, macOS, and Windows absence rollback operations validate failures while retaining authoritative already-absent behavior.

Runtime rollback records process status and readiness separately, then uses bounded status polling around stop/start restoration so PID-file removal and delayed transitions cannot be mistaken for confirmed process state.

Native init holds the profile start lock across provisional persistence, supervisor installation/RunAtLoad, strict startup, and final persistence. The provisional manifest is saved before supervisor installation, and the strict inner start uses the already-held lock rather than reacquiring or bypassing it.

### GREEN

- Full init/supervisor suite: 148 passed.
- Focused native suite: 51 passed, 65 deselected.
- Ruff format/check on all four scoped files: passed.
- mypy on both changed production files: passed.
- `git diff --check`: passed.

## Fix Round 5

### RED

- Added a live-process regression using a real Python child that delays exit after SIGTERM. Before the fix, `stop_runtime()` returned and cleared its PID file while the child was still alive.
- Added a ready-runtime rollback regression with a non-reentrant fake start lock while leaving `_start_profile_strict` unmocked. It exposed rollback attempting to reacquire the lock held by `_init_native_hosts`.

### Corrections

`stop_runtime()` now polls the targeted PID for up to 10 seconds after SIGTERM, clears the PID file only after confirmed death, and raises while retaining the PID file when the process does not exit. Ready-runtime rollback accepts the already-held native-init start lock and calls the locked strict-start path instead of reacquiring it.

### GREEN

- Full init/supervisor/runtime suite: 177 passed.
- Focused Fix Round 5 regressions: 3 passed.
- Ruff format/check on all five covered files: passed.
- mypy on both changed production files: passed.
- `git diff --check`: passed.
