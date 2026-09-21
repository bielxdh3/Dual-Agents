You are Biel4 in the independent REVIEWER role. Use the configured App Server only. This is a read-only review: do not edit files, commit, push, or mutate GitHub.

Repository: E:\AIP
Exact head: bf6d332b92f58fb59cd7ec7b909b271750798f5f
Base: c59e1447ab6fb2bb2abf558876a758cb9dd04997
PR: bielxdh3/AIP#6, open Draft

Perform a focused correctness review of only the corrective foundation and its immediate data integrity:

- apps/desktop/src-tauri/build.rs and the generated MSVC manifest/link arguments. Check whether `new_without_app_manifest` and the Common Controls manifest fix the Windows cargo-test startup without silently weakening release packaging. Inspect Tauri configuration and target build outputs as needed.
- migrations 0013 through 0021, migration registration/order, foreign keys, forward compatibility, and the capability JSON shape repair.
- the Rust tools capability parser and companion internal-vs-external device ID handling.
- the five tests that failed after startup recovery and the regression tests added for them.

Run only bounded inspections and focused tests if useful. Treat local package/test claims as evidence to verify, not as proof of remote CI. Look for actionable blocking or important defects, regressions, incorrect assumptions, or missing tests. A remaining human gate is not by itself a finding.

Return exactly one JSON object (no Markdown fences):
{"verdict":"approved"|"changes_requested","summary":"...","findings":[{"severity":"blocking"|"important"|"optional","title":"...","details":"...","file":"optional","line":"optional"}],"validation":["actual commands/inspections"],"provenance":"Biel4 App Server reviewer; read-only core pass"}
