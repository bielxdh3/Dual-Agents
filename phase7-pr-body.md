## Summary

Completes the Phase 7B-7F cognitive-core workflow on top of the existing 7A foundation.

- Fail-closed `memory:<id>`, `conversation:<id>`, and `message:<id>` source validation with Owner/agent lineage and completed-public-conversation checks.
- Transactional memory invalidation preserving history, superseding dependent records, and deterministically recomputing opinion and six-dimension relationship projections.
- Deterministic fictional goal expiry and bounded activity expiry/lifecycle controls, with temporary-chat and external-action guards.
- Owner-visible provenance, all relationship dimensions, goal schedule/evidence, and fictional activity controls with focused frontend coverage.
- Required Phase 7 specification, roadmap, gap matrix, data model, limitations, and validation docs updated while historical blocked evidence remains retained.

## Validation

- `cargo fmt --manifest-path apps/desktop/src-tauri/Cargo.toml --check`
- `cargo check --locked --manifest-path apps/desktop/src-tauri/Cargo.toml`
- `cargo clippy --locked --manifest-path apps/desktop/src-tauri/Cargo.toml --all-targets -- -D warnings`
- `cargo test --locked --manifest-path apps/desktop/src-tauri/Cargo.toml --lib` — 147 passed, 1 ignored (Ollama model)
- Contracts typecheck/build/test — 20 tests passed
- Desktop typecheck/build/test — 14 files, 51 tests passed
- Python format/lint/mypy/unittest — 27 tests passed
- `pnpm secrets:scan` — 168 files checked, passed
- Focused UI ESLint and `git diff --check` — passed
- Codex Security diff scan `da9a36fd-c845-4d62-bf31-63f595eb4133` — complete coverage, 0 findings
- Distinct Biel4 Reviewer via App Server — `Approved with reservations`

## Known reservations

- Installed-Windows interaction, packaged restart/Owner smoke, and subjective Portuguese/accessibility review remain `HUMAN_VALIDATION_REQUIRED`.
- Remote CI must be green before merge. Android Gradle was not run locally because `JAVA_HOME` is absent; global lint/prettier retain unrelated baseline failures.
- No stable release/tag is created.
