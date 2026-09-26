# Dual Agents App integration

The Codex App is the user-facing entrypoint. When a request or attached
mission asks for Dual Agents/Dual Codex, independent Architect/Executor/
Reviewer roles, the Dual Agents lifecycle, or includes a blocker that depends
on another role's availability, route the mission to the Dual Agents control
plane. Do not require a fixed phrase or decide role availability from the
visible thread's tools.

This intake routing applies only before the first handoff. If a trusted
bootstrap marks the current turn as a configured role phase, follow its
phase-specific task and output schema without re-entering the control plane.

For a complete multi-phase mission, invoke `dual-codex run` (or the installed
global launcher) from the target repository and pass its explicit repository
path. The control plane resolves every role from config, the provider
capability matrix, and live runtime checks. Read the resulting report,
`provenance.json`, and target diff. Keep configured blockers intact; never
replace a missing or unavailable role with the visible Codex.

Use `delegate` only for a bounded single Executor implementation/correction
request with its versioned JSON request and result file. Do not use `terminal
list` or `terminal start` to launch configured mission actors. Those commands
only manage native Windows Codex TUI sessions. Do not print or read
authentication files.

When changing the Dual Agents orchestration, work directly in a Codex session;
do not use the orchestration system under repair to implement or validate
itself.
