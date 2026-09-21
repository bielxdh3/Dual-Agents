# Biel4 Executor correction from independent Reviewer

Read the mandatory global/project/control-plane AGENTS.md files and required skills (`aip-implementation`, `aip-phase-review`, `aip-security-review`, `aip-publication-check`, `ponytail`) before acting. Work directly in `E:\AIP` on `fix/productize-local-capabilities`, preserving commit `6da72b0` and unrelated scope. Use App Server/headless Code mode; no fallback.

Correct only these blocking findings from the distinct Biel4 Reviewer, with tests:

1. Screen Vision: the selected privacy policy must be enforced before real pixels reach a local provider. Implement a conservative fail-closed redaction boundary that does not claim `redactionApplied` unless pixels were actually transformed; synthetic fixtures remain metadata-only and real previews must truthfully report their source. Add deterministic Rust coverage for the boundary and preserve explicit confirmation, cancellation, transient/no-persistence, and uncertain output.
2. Workspace roots: `add_workspace_root` and `remove_workspace_root` must not use a hardcoded agent to check temporary chat. Bind the request to the active/requesting agent (or an equivalent authoritative Owner-scoped guard) so a temporary Luma chat cannot mutate durable roots. Update the TypeScript calls and add a regression test.
3. Local status cards: make capability cards actionable. Clicking a card must open/focus its target details panel (not only jump to a closed `<details>`), and Runtime/Ollama must lead to an understandable configuration/status destination. Keep Portuguese copy compact.
4. Quality: fix the new `no-control-regex` lint violation without weakening validation, and run rustfmt on the correct manifest path. Do not mass-format unrelated files. Run desktop typecheck, contracts tests, desktop tests, `pnpm lint` if feasible, `cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --check`, cargo check and focused Rust tests. Commit one correction as `fix: close productization review blockers`.

Do not implement provider registry, Android transport, Gateway external client, packaging, push, PR, merge, tag, release, or docs in this correction. Do not reset/discard. Return one JSON object with summary, files_changed, commands_run, tests, remaining_issues.
