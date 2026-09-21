# Biel4 Executor — provider registry and setup boundary

Read the mandatory AGENTS.md files and all required AIP/ponytail skills before acting. Work directly in `E:\AIP` on `fix/productize-local-capabilities`, starting from the latest local correction commit. Use Biel4 App Server/headless Code mode with provenanced executor metadata; no fallback.

Implement the smallest safe, restart-persistent local provider registry required by the mission for Voice and Screen Vision. Do not weaken any existing safety boundary. Requirements:

- Add one forward SQLite migration and authoritative Rust storage for bounded local provider records: provider id, kind (stt/tts/visual), display name, canonical absolute executable path, protocol version, enabled, validation status/result, timestamps; reject secrets, shell command strings, relative paths, non-files, and non-Windows executables where applicable. Preserve env vars only as optional developer overrides, never the sole ordinary setup path.
- Add typed Tauri commands to list, register/validate, and remove/disable providers. Registration must be Owner-scoped, idempotent, temporary-chat/safe-mode blocked, and use existing bounded protocol validation (a safe executable path is not enough to claim ready). No arbitrary arguments.
- Make VoiceControls show a provider list, visible display name/kind/status, register/update/remove controls using a supported local path field, and save selected provider references without requiring strings such as local:stt:provider. Keep fixture controls clearly synthetic. Make real status use registry first and degrade honestly.
- Make Screen Vision provider status use registry first; expose provider selection/configuration in its panel; never claim a real provider is ready from env-only metadata. Keep real capture explicit and fixture path distinct.
- Add deterministic Rust/contract tests for path validation, registry lifecycle/restart, missing/invalid provider, and provider status parsing. Update Portuguese copy and docs only for this registry behavior; no broad redesign.
- Do not implement Android transport, Gateway external client, packaging, push, PR, merge, tag, release, or stable release. Commit one local changeset as `feat: add local provider registry` after focused tests (desktop typecheck/contracts tests/cargo fmt/cargo check).

Return one JSON object with summary, files_changed, commands_run, tests, remaining_issues.
