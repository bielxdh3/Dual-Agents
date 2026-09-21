## Summary

- Rebuild the desktop navigation around agents and their conversations, removing the permanent main-conversation UX.
- Add conversation CRUD, pin/archive/delete flows, contextual temporary chat, per-conversation model selection with agent defaults, and PT-BR-first profile labels.
- Make local resources and voice diagnostics compact, preserve legacy conversation data/settings across restart, and harden overlay transparency/hit regions.
- Document the future bundled/managed offline STT/TTS direction for Windows MSI and Android APK without claiming it as current functionality.

## Validation

- `pnpm secrets:scan`
- `pnpm lint`
- `pnpm typecheck`
- `pnpm test` (23 contract + 62 desktop tests)
- `pnpm build`
- `pnpm python:check` (27 runtime tests)
- `pnpm tauri:check` (155 Rust tests, 1 Ollama-dependent ignored)
- `scripts/build-runtime.ps1` sidecar smoke

Android validation is covered by the repository CI job; local execution was unavailable because this host has no Java/JAVA_HOME.

## Human-only acceptance gate

The merged desktop installer still needs a real Windows smoke test for sidebar/conversation CRUD, temporary chat, model/profile controls, compact local resources/voice, overlay transparency/click-through, and restart persistence. No authenticated or installed-Windows ceremony is claimed by source or CI checks.
