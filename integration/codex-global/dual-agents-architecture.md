## Dual Agents architecture

<!-- DUAL_AGENTS_GLOBAL_ARCHITECTURE_BEGIN -->

The Codex App is the user-facing entrypoint. The Dual Agents control plane
resolves Architect, Executor, Reviewer, and Orchestrator roles from the
registered configuration, provider capability matrix, and live runtime.
Role availability must not be inferred from the visible thread's tools.

### Natural mission routing

When a request or attached mission asks for Dual Agents/Dual Codex, independent
configured agent roles, the Dual Agents lifecycle/protocol, or contains a
blocker that depends on another role's availability, load
`C:\CodexGlobal\skills\dual-agents\SKILL.md` and use the installed official
entrypoint. No fixed activation phrase is required. This applies when the
Codex App is opened in a repository outside the Dual Agents source tree.

This routing applies only to the user-facing entrypoint before its first
handoff. When a trusted bootstrap identifies the current turn as a configured
Architect, Executor, or Reviewer phase, follow that phase's task and output
schema; do not invoke the entrypoint recursively.

For a complete mission, invoke `dual-codex run` from the target repository
with its Git root passed explicitly. The configured control plane owns role
resolution and fails closed if a required profile, capability, or runtime is
missing. Preserve blockers and provenance; do not substitute a single visible
Codex or an unconfigured backend.

### Role responsibilities

- The configured Architect plans the mission.
- The configured Executor implements the bounded plan.
- The configured Reviewer reviews the resulting change.
- The control plane records configured and actual actor, provider/backend,
  runtime/session identity, repository identity, and fallback state.

Provider support is defined by config, the provider capability matrix, and
runtime readiness. Documentation and entrypoint instructions must not
hard-code one Executor provider when the implementation supports others.

### Self-modification rule

Do not use Dual Agents to repair, refactor, replace, or validate the Dual
Agents orchestration implementation itself. Such changes must be performed
by a direct Codex session.

<!-- DUAL_AGENTS_GLOBAL_ARCHITECTURE_END -->
