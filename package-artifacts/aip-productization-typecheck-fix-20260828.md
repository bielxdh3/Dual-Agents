# Biel4 Executor focused correction

Read the mandatory global/project/control-plane AGENTS.md files and the five required skills before acting. Work directly in `E:\AIP`, branch `fix/productize-local-capabilities`, preserving the existing dirty diff. This is a narrow recovery task after the launcher timed out before starting a turn.

Inspect the existing diff only. Fix the current `pnpm --filter @aip/desktop typecheck` errors and any directly-caused import/typing issues, without redesigning capability workflows or touching unrelated files. Keep safe-mode and temporary-chat gates intact. Run the desktop typecheck and contracts test plus `cargo fmt --manifest-path apps/desktop/Cargo.toml --check` if applicable. Make one local commit only if changes are needed, with message `fix: validate local capability surface`. Do not push, create a PR, merge, package, or use a fallback. Return one JSON object with summary, files_changed, commands_run, tests, remaining_issues.
