## Summary

Fixes the five Owner-observed installed-Windows Big Smoke UI/editor regressions while preserving the existing AIP Phase 7-13 behavior.

## Root causes and fixes

1. **Sprite transparency** — `AgentSprite` passed `color` as an SVG attribute rather than `fill`, so overlay pixels rendered with the default opaque black paint. PNG import also discarded partial alpha. The fix uses explicit SVG `fill`/coordinates and preserves non-zero RGBA as `#RRGGBBAA`.
2. **Giant square/bounding box** — the compositor's default opaque rects created the apparent square; overlay/Tauri transparency rules were already correct. Alpha-aware compositing removes the unintended visible region while editor-only canvas styling stays scoped.
3. **Pencil corruption** — the editor mutated `activeLayer.pixels` in place before serializing. The fix creates immutable layer updates through a shared pixel helper, isolating unrelated layers and preserving undo/redo.
4. **Selection anchor** — drag updates reused the existing selection rectangle as the anchor. A pointer-down anchor ref and bounded rectangle helper now support arbitrary starts, reverse drags, repeated selections, edge clamping, and cancel/end reset.
5. **Raw conversation HTML** — `ConversationList` had markup but no matching scoped styles or accessible input labeling. Semantic class hooks, hierarchy/spacing, focus/hover/disabled states, and accessible labels restore the intended desktop styling.

## Tests and validation

- 4 focused regression files: 12 tests passed.
- Full workspace tests: contracts 17 passed; desktop 56 passed.
- TypeScript typecheck passed.
- Production Vite build passed.
- Python format/lint/typecheck/tests passed (27 tests).
- `cargo fmt --check`, `cargo check`, Clippy `-D warnings`, and Rust/Tauri tests passed (120 passed, 1 ignored).
- `pnpm secrets:scan` passed (150 repository files checked).
- Touched-file ESLint passed; global lint still reports the pre-existing `no-control-regex` issue at `packages/contracts/src/index.ts:2606`.
- Touched-file Prettier check reports pre-existing formatting in large mixed-format files; net diff was narrowed to the requested hunks.
- Local Windows release build produced MSI and NSIS installers.

## Scope

- No dependency upgrades, unrelated runtime changes, BielOS changes, tags, or releases.
- Native installed-Windows visual confirmation remains an Owner gate after the merged installers are available.
