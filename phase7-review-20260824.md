# AIP Phase 7B-7F independent Reviewer task

You are the distinct Biel4 Reviewer running through the configured Biel4 App Server. This is read-only: do not edit files, create commits, change branches, run publication actions, or use native subagents.

Repository: `C:/Users/bielx/3D Objects/Dual-Codex-worktree/aip-phase10-checkout`
Branch: `feat/phase-7b-7f-functional-closure`
Base: `f9e5df1fb32a40f4959b29f20d702554f4089ea7`
Head: `0e458203c665ebad3985e1e7c34979feb214e33c`

Review the complete diff from base to head, including:

- `apps/desktop/src-tauri/src/cognitive.rs`
- `apps/desktop/src-tauri/src/database.rs`
- `apps/desktop/src/App.tsx`
- `apps/desktop/src/cognitive-panel.test.tsx`
- the seven required Phase 7 docs

Acceptance scope is only Phase 7B-7F. Check that 7B opinions, 7C six-dimension relationships, 7D fictional goals/activity expiry and lifecycle, and 7E public conversations are real bounded workflows rather than metadata. Verify fail-closed owned confirmed-memory and completed-public-conversation/message source references, transactional memory invalidation that supersedes history and recomputes opinion/relationship projections, deterministic goal/activity expiry, temporary-chat and external-action gates, public conversation consent/revocation/termination/budget boundaries, and Owner-visible provenance/activity controls. Confirm later phases are not materially changed and the App.tsx diff is minimal.

Use current evidence only: Rust format/check/clippy/full lib test passed (147 passed, 1 ignored), focused cognitive tests passed (10), contracts typecheck/test/build passed (20 contract tests), desktop typecheck/test/build passed (14 files/51 tests), Python checks passed (27), secrets scan passed, and focused UI ESLint passed. Global lint/prettier have known unrelated baseline failures; Android Gradle was not runnable because JAVA_HOME is absent. A Codex Security diff scan over all four changed source files completed with zero findings, complete coverage, but parent-only worker fallback and disconnected TAC connector.

Return a concise report with:

1. One exact verdict: `Approved`, `Approved with reservations`, `Hotfix required`, or `Rejected`.
2. Blocking findings, important findings, and optional notes, each with file and line references where applicable.
3. Scope and later-phase regression assessment.
4. Validation evidence you independently checked and any remaining HUMAN_VALIDATION_REQUIRED gates.
5. No edits or publication.
