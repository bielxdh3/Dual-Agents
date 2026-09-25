---
name: dual-agents
description: Route user-facing missions through the Dual Agents control plane when they ask for Dual Agents or Dual Codex, require independently configured Architect, Executor, or Reviewer roles, reference its run/delegate lifecycle, or include multi-actor role/runtime blockers. This skill is for intake before dispatch, not for an actor already running a trusted phase.
---

# Dual Agents mission entrypoint

The visible Codex thread is the entrypoint. It does not need to host every
configured actor, and its available tools are not evidence that a configured
role is missing.

This skill applies to the user-facing intake turn before the first control
plane handoff. If a host-trusted bootstrap marks the current turn as a
configured Architect, Executor, or Reviewer phase, follow that phase's task
and output schema. Do not call the launcher recursively from an active phase.

Use this skill when the request or attached mission indicates any of the
following, even without a fixed phrase:

- Dual Agents or Dual Codex;
- independent or separately configured Architect, Executor, Reviewer, or
  Orchestrator phases;
- the Dual Agents run/delegate lifecycle or protocol;
- a blocker whose answer depends on whether another role or runtime exists.

For a complete multi-phase mission:

1. Identify the intended target repository from the active workspace or the
   mission. Resolve its Git root with `git rev-parse --show-toplevel`. Keep the
   target path explicit; never infer it from the installed Dual Agents source
   tree or from the repository recorded in a global config.
2. Use the attached Markdown mission when it is available as a file. If it is
   only present in the conversation, write it to a uniquely named temporary
   `.md` file with a filesystem API, not a shell-quoted prompt string, and
   remove that exact file after the run.
3. Read `C:\CodexGlobal\dual-agents-integration.json` to confirm the one-time
   installation registration, then invoke the registered wrapper from the
   target repository, passing its Git root explicitly:

   ```powershell
   & 'C:\CodexGlobal\bin\dual-codex.ps1' run --repository $targetRoot $taskFile
   ```

   The wrapper supplies the registered config and launcher. Do not switch the
   working directory to the Dual Agents source repository. Use `run` for the
   Architect → Executor → Reviewer lifecycle; use `delegate` only when the
   mission itself asks for one bounded Executor implementation/correction.
4. Let the real control plane resolve each role from the registered config,
   provider capability matrix, and live runtime. Do not compare those roles to
   the visible thread's tools, start a second arbitrary Codex, impersonate an
   actor, weaken blockers, or offer single-agent execution as a workaround.
5. Read the final run report, target Git state/diff, and `provenance.json`.
   Confirm the target repository identity and the configured/actual actor,
   provider, backend, runtime/session identity, and fallback state for every
   phase. Report a missing or unavailable role only when the control plane
   returns that failure. Preserve its failure details and provenance; stop
   without attempting a substitute.

If the registration, config, launcher, or task artifact cannot be resolved,
report that concrete entrypoint/setup blocker and leave the task unexecuted.
Never claim that the visible thread's own role inventory is the control
plane's role-resolution result.
