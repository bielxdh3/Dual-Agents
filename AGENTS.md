# Dual Agents App integration

The Codex App is the visible Codex Architect, reviewer, and user interface.
Google Antigravity/Gemini is the only active `executor` backend for delegated
implementation work. Do not substitute a Codex Executor or another backend.

When the user says “Use Dual Agents to implement this task.”:

1. Inspect and understand the target repository in the visible App.
2. Prepare a precise version-1 JSON request with `action: "implement"` and an
   explicit `repository` path.
3. Run the repository-local launcher:

   ```powershell
   .\scripts\dual-codex.ps1 --config <config> delegate --request-file <request> --result-file <result>
   ```

   Standard input is also supported with `delegate --stdin --result-file`.
4. Wait for the final `DUAL_CODEX_RESULT` line, then read the result JSON,
   executor report, Git status, and diff named by that result.
5. Review the real implementation in the visible App. Create a version-1
   `correct` request only for concrete blocking or important findings. A
   correction must include the original task, `parent_request_id`, and
   actionable `review_findings`.
6. Respect `max_correction_cycles` from configuration and present the final
   evidence to the user. Never claim success without reading the result and
   diff.

For a mission that runs the configured Architect, Executor, and Reviewer
roles, use the provider-aware `run` command:

```powershell
.\scripts\dual-codex.ps1 --config <config> run <task-file>
```

Do not use `terminal list` or `terminal start` to launch or validate configured
mission actors. Those commands only inspect or start native Windows Codex
sessions. `run` resolves each role through its configured profile and backend;
review `provenance.json` to verify the selected actor, runtime, and fallback
state. Unsupported role/provider combinations fail closed.

Before delegating, use `status --json` when useful to verify the executor role,
executor label, Antigravity/Gemini backend and `agy` status, the active
repository, Git state, and CLI versions. Delegation refuses an unassigned,
non-Antigravity, or unavailable Executor; it never falls back silently. Do not
invoke the visible Architect account through `codex exec` for the same
delegation, and do not print or read authentication files.
