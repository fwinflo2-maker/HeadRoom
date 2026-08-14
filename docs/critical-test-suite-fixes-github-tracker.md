# Critical Test-Suite Fixes and GitHub Tracking

Date: 2026-08-13

## Scope

This inventory covers the critical suite-repair work on the pushed `thermi`
branch, from the last shared upstream point (`8af6b936a`) through `HEAD`.
Unrelated Docker, Serena-memory, and planning files in the worktree are not
included.

GitHub searches were performed against the canonical upstream repository,
`headroomlabs-ai/headroom`. The configured fork `Thermi/headroom` has GitHub
Issues disabled, so its issue tracker cannot provide issue matches.

## Local Critical-Fix Inventory

| Commit | Area | Fix |
| --- | --- | --- |
| `e9451232d` | Windows / RTK | Make forked RTK hook tests portable and avoid shell-child timeout behavior. |
| `a6cd66d57` | DeepSeek / tokenization | Avoid request-time DeepSeek HuggingFace tokenizer downloads; use the estimator for gateway aliases. |
| `e35db6ab3` | Cross-platform suite | Repair preload, retry timing, SSE validation, beacon timing, subscription rendering, interceptor state, SQLite cleanup, relevance routing, and portability failures. |
| `f18e4bc9d` | Serena / Windows | Honor an explicitly-set `HOME` when locating Serena configuration. |
| `8660bb665` | Tests / portability | Make TOIN paths separator-neutral and guard tag invariants when no tags are protected. |
| `87d1ea746` | Rust / CCR | Propagate `enable_ccr_marker` into the Rust document-compaction classifier. |
| `1b3e3b0c7` | SQLite / Windows | Close cached vector and graph-store connections before Windows teardown. |
| `76fa4a0b0` | Ledger / concurrency | Serialize Windows savings-ledger appends and compaction with a process-local lock. |
| `7e0e1fa84` | Transform compatibility | Validate Tree-sitter with a real parse, restore TextCrusher config, and preserve tool-result routing behavior. |
| `d7e5ffbae` | Streaming / warmup | Restore OpenAI streaming tracker compatibility and keep `optimize=False` warmup slots null. |
| `3f913e8fb` | Savings history | Preserve the v3 public history contract while storage evolves to schema v5. |
| `59270af77` | OpenAI cache | Tolerate partial prefix-tracker test/custom implementations. |
| `e7b53e02e` | Savings profiles | Keep explicit `protect_recent` and read-protection settings aligned. |
| `3e550edcc` | Kompress health | Avoid promoting cache state without a live runtime compressor. |
| `ed65d89e5` | Kompress health | Preserve deferred warmup status during cache promotion. |
| `5d3b2a79c` | Kompress health | Keep pending attached Kompress models unhealthy until ready. |
| `d5ef46462` | Health / debug | Separate debug and health Kompress reconciliation. |
| `5362c0a44` | Kompress health | Preserve explicit Kompress health states. |
| `9a8178e02` | Batch handlers | Preserve Google/OpenAI batch-handler compatibility seams. |
| `88d39b628` | Extensions | Distinguish optional extension failures from enabled extension failures. |
| `4a233372e` | Kompress warmup | Reconcile deferred Kompress warmup state. |
| `f40dc5c3c` | Kompress startup | Defer Kompress preload when optimization is disabled. |
| `d20b8af86` | Context tools | Route RTK/lean-ctx stats through the UTF-8 subprocess wrapper. |
| `605de77f7` | Dashboard | Restore context-tool dashboard statistics. |
| `148aef501` | Rate limits | Validate enabled RPM configuration. |
| `efb1fa0c4` | Qdrant | Use a safe Qdrant port configuration factory. |
| `00d5870f9` | Codex | Stamp the Codex desktop response client field. |
| `62acb5b52` | Trackers | Preserve legacy tracker factory calls. |
| `167ea55fc` | CLI settings | Register all proxy Click settings. |
| `843ff3383` | Request logs | Write redacted request logs as plain JSONL. |
| `411b0a154` | Optional dependencies | Isolate optional LiteLLM Vertex backend tests. |
| `0c4051cab` | AST-grep | Keep the AST-grep wrapper hidden from subprocess audits. |
| `f63cbc381` | AST-grep | Preserve the AST-grep subprocess patch seam. |
| `b708cb189` | Reloads | Preserve all tracker classes across helper reloads. |
| `18512b550` | Reloads | Preserve tracker classes across helper reloads. |
| `0eb71aeb3` | Extensions | Propagate enabled extension installation errors. |
| `798ae40e1` | CLI summary | Restore CLI token fields in session summaries. |
| `b29cffc3c` | Cost history | Accept legacy cost-history entries. |
| `9d2f5615b` | Cost history | Normalize merged cost statistics. |
| `8fceccba1` | CCR | Restore the residual CCR status contract. |
| `a9c445999` | CCR cache | Preserve deferred CCR cache prefixes. |
| `2e323466b` | DeepSeek | Correct DeepSeek context fallback limits. |
| `0d32e9f5f` | DeepSeek tests | Skip the DeepSeek resolver test when LiteLLM is unavailable. |
| `8bd666590` | Proxy routes | Honor disabled Anthropic proxy routes. |
| `0670eeccc` | Windows tests | Skip POSIX permission probes on Windows. |
| `40655f78a` | Process probes | Isolate signal-zero PID probes. |
| `5091a5095` | Anthropic batch | Restore the Anthropic batch executor seam. |
| `86f4eb419` | Versions | Mark source-tree versions as development versions. |
| `ceb847cd7` | Dashboard | Preserve dashboard asset line endings. |
| `cbf4fa7de` | Optional deps | Preserve optional pricing and backend test isolation. |
| `e7d4d3bef` | Memory | Handle malformed memory tool calls. |
| `b92b9dc01` | Memory | Preserve similar memories on save. |
| `61b1cea43` | Memory | Initialize local memory backends once. |
| `37697654e` | Claude | Skip null Claude transcript messages. |
| `6e538d439` | Windows tests | Fix Windows learn-path coverage. |
| `3996d0991` | Learn tests | Isolate learn-plugin detection. |
| `76c25d7dd` | Learn | Normalize learn-writer trailing newlines. |
| `b142ece5e` | Learn tests | Normalize analyzer project-path assertions. |
| `fbacfd027` | Tests | Synchronize background download threads. |
| `4b274a5c9` | Tests | Align supervisor subprocess contracts. |
| `9b384becd` | Install | Align install-mode defaults. |
| `4658f4499` | OpenCode tests | Scope OpenCode configuration isolation. |
| `c621b1d58` | PERF | Retain in-memory PERF records. |
| `135d99206` | Request logs | Flush compressed request logs before reading them. |
| `13139ec6a` | Image compression | Align image-compressor lifecycle contracts. |
| `ffc6f8c76` | Hooks | Restore suite and hook contracts. |
| `2e9c38b01` | Windows tests | Account for Windows file-mode semantics. |
| `336b69366` | Hooks | Repair the Windows hook environment. |
| `86a7e82fc` | Dashboard tests | Use stable dashboard template IDs. |
| `fcd8beb71` | Golden tools | Fail open on corrupt golden tool bytes. |
| `257174156` | DeepSeek | Map generic DeepSeek V4 to a usable tokenizer. |
| `d0f8e1584` | Copilot | Normalize Copilot exchange hosts. |
| `1c3382283` | Windows tests | Skip privileged Windows symlink cases. |
| `10cb64c60` | CCR | Align MCP retrieval with hash-only CCR. |
| `01f6e33f4` | OpenCode tests | Isolate OpenCode configuration writes. |
| `593309d3b` | Code compression | Preserve Go function-block syntax. |
| `ae014425e` | Update tests | Align the update-notice arrow. |
| `6c4764add` | CLI | Separate bundled-tool doctor columns. |
| `cbc3cad02` | Retry | Expose proxy retry-delay options. |
| `1b576f350` | CCR | Propagate CCR marker policy to the search compressor. |
| `fcba2ead3` | Windows | Report Windows-reserved proxy ports. |
| `428ee47cc` | OpenCode tests | Isolate OpenCode configuration writes. |
| `ba5d6b934` | Retired behavior | Remove retired OpenHands RTK cases. |
| `8af6b936a` | Retired behavior | Remove retired RTK hint-file coverage; inventory base. |

## Verified Upstream Pull Requests

These are upstream PRs whose titles/bodies directly match one or more fixes in
the local inventory. Links point to the canonical repository.

| PR | Status | Relevant result |
| --- | --- | --- |
| [#2743](https://github.com/headroomlabs-ai/headroom/pull/2743) | Merged | `/v1/compress` resolves tokenizers per model instead of pinning one provider counter; directly matches the local tokenizer-counting fixes. |
| [#2761](https://github.com/headroomlabs-ai/headroom/pull/2761) | Merged | Gives every model exactly one tokenizer and addresses gateway/provider tokenizer disagreement. |
| [#2758](https://github.com/headroomlabs-ai/headroom/pull/2758) | Merged | Fixes HuggingFace chat-template counting, GPT-5 coverage, and gateway-wrapped model names. |
| [#2801](https://github.com/headroomlabs-ai/headroom/pull/2801) | Merged | Coerces non-string tool-call fields before token counting. |
| [#2838](https://github.com/headroomlabs-ai/headroom/pull/2838) | Merged | Covers token-count memoization and startup preload behavior. |
| [#2799](https://github.com/headroomlabs-ai/headroom/pull/2799) | Open | Warm deferred Kompress models so health does not remain `backend=null`; directly related to the local warmup/reconciliation fixes. |
| [#2448](https://github.com/headroomlabs-ai/headroom/pull/2448) | Merged | Matches all ONNX backend names, relevant to Kompress warmup/backend detection. |
| [#2980](https://github.com/headroomlabs-ai/headroom/pull/2980) | Open | Consolidates Windows installation fallback and cleanup safety; closely matches the Windows lifecycle/test repairs. |
| [#2676](https://github.com/headroomlabs-ai/headroom/pull/2676) | Merged | Stops creating partial Serena config files and repairs existing ones; directly matches the Serena fix. |
| [#2677](https://github.com/headroomlabs-ai/headroom/pull/2677) | Merged | Removes retired RTK and lean-ctx CLI context tools; matches retired-tool test cleanup. |
| [#2953](https://github.com/headroomlabs-ai/headroom/pull/2953) | Merged | Avoids buffering a CCR stream when passthrough discards the stream flip. |
| [#2931](https://github.com/headroomlabs-ai/headroom/pull/2931) | Merged | Avoids injecting the CCR tool on chat streaming. |
| [#2908](https://github.com/headroomlabs-ai/headroom/pull/2908) | Merged | Verifies a scanned CCR marker hash before advertising it. |
| [#2848](https://github.com/headroomlabs-ai/headroom/pull/2848) | Merged | Injects `headroom_retrieve` whenever a CCR marker is present. |
| [#2703](https://github.com/headroomlabs-ai/headroom/pull/2703) | Merged | Stops persisting retrieval markers as original content; PR body references issue #2694. |
| [#2698](https://github.com/headroomlabs-ai/headroom/pull/2698) | Merged | Honors qualified CCR names across integrations. |
| [#2756](https://github.com/headroomlabs-ai/headroom/pull/2756) | Merged | Stops mixing tokenizer scales in `RequestOutcome`; matches token/accounting fixes. |
| [#2891](https://github.com/headroomlabs-ai/headroom/pull/2891) | Closed with conflicts | Telemetry/TOIN persistence optimization; relevant to ledger and telemetry fixes, but not mergeable as listed. |
| [#2951](https://github.com/headroomlabs-ai/headroom/pull/2951) | Merged | Sanitizes malformed `entity_refs`; related to memory robustness fixes. |
| [#2579](https://github.com/headroomlabs-ai/headroom/pull/2579) | Merged | Bounds the TrafficLearner pending-pattern accumulator. |
| [#2266](https://github.com/headroomlabs-ai/headroom/pull/2266) | Closed / superseded | Explicitly addresses stale CI test failures on main. |
| [#2155](https://github.com/headroomlabs-ai/headroom/pull/2155) | Merged | Scopes CI native/wheel/dashboard jobs to relevant paths. |
| [#2522](https://github.com/headroomlabs-ai/headroom/pull/2522) | Open with conflicts | Guards learn session-derived path checks against `PermissionError`. |

## Verified Issue Tracker Results

The upstream issue search surfaced these relevant issue records:

| Issue | Status | Relevance |
| --- | --- | --- |
| [#1038](https://github.com/headroomlabs-ai/headroom/issues/1038) | Closed / completed | DeepSeek context limit was hard-coded at 128K instead of 1M; matches the local DeepSeek context fallback correction. |
| [#953](https://github.com/headroomlabs-ai/headroom/issues/953) | Open | Windows 11 Claude Code plus DeepSeek API trouble; relevant context for the Windows/DeepSeek fixes, but not an exact one-to-one match. |
| [#1011](https://github.com/headroomlabs-ai/headroom/issues/1011) | Closed / not planned | Cache misses with Reasonix and DeepSeek V4 Pro/Flash; related to the local cache/warmup work but explicitly not planned upstream. |
| [#980](https://github.com/headroomlabs-ai/headroom/issues/980) | Open | Proxy hangs on first Docker request due to synchronous DNS/HTTP; related to startup/offload concerns, not directly a local test fix. |
| [#1832](https://github.com/headroomlabs-ai/headroom/issues/1832) | Open | DeepSeek separation and reporting feature request; related provider domain context, not a direct test regression. |

## Search Limitations and Non-Matches

- `Thermi/headroom` reports `has_issues=false`; its issue search returned no
  usable issue records.
- GitHub API search requests against the canonical repository intermittently
  returned HTTP 403 rate-limit responses after the first successful searches.
- The browser issue search was used for verified issue titles/statuses where API
  search was unavailable.
- No exact upstream issue/PR match was found for every local commit. In
  particular, the local-only compatibility commits for savings-history schema,
  partial prefix trackers, SQLite test teardown, TOIN path separators, and
  tag-invariant test guards were not individually traceable to a numbered
  upstream issue from the accessible tracker results.
- No exact PR title containing `SSE` was found in the accessible upstream PR
  listings; the streaming matches were recorded under CCR/WebSocket PRs.
- No exact PR title containing `Kompress warmup` was found; PR #2799 is the
  closest verified match and is still open.

## Conclusion

The strongest upstream linkage is around tokenizer correctness (#2743, #2758,
#2761, #2801), Kompress startup/health (#2799, #2448), CCR streaming and marker
integrity (#2848, #2908, #2931, #2953), and Serena/Windows installation safety
(#2676, #2677, #2980). The remaining local suite fixes are primarily branch
realignment, compatibility, and Windows test-environment repairs without a
public numbered issue match.
