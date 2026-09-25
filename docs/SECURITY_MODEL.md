# Security Model

This document describes the intended security boundaries of Dual Agents. It is a design contract, not a claim that the project is free of vulnerabilities.

## Security goals

Dual Agents should:

- route each role only to a provider/backend that explicitly supports it;
- fail closed when the configured actor is unavailable or unsupported;
- keep provider authentication and state isolated according to the provider adapter;
- keep workspace access within the configured repository boundary;
- avoid exposing secrets through logs, prompts, run artifacts, or provenance;
- make the configured and actual actor inspectable;
- preserve enough evidence to audit fallback and execution behavior;
- keep local management surfaces local unless an operator deliberately changes that boundary.

## Non-goals

Dual Agents does not attempt to make arbitrary model output trustworthy.

It also does not make an unsafe target repository, provider runtime, shell command, plugin, dependency, or external API safe merely by routing it through the control plane.

## Trust boundaries

### User and configuration

Configuration selects profiles, roles, backends, models, repositories, and optional fallback behavior. Configuration is security-sensitive input and should not be accepted from untrusted sources without review.

### Provider authentication

Provider-owned authentication should remain with the provider runtime or the explicitly configured secret reference.

The control plane should not copy authentication state between providers or silently reuse one profile as another.

### Repository workspace

A configured repository is the intended filesystem boundary for repository work. Path handling must not allow a task, generated output, archive, or provider response to escape that boundary unexpectedly.

### Provider/runtime adapters

Codex, Antigravity/Gemini, Claude Code, and API adapters have different capabilities. The adapter boundary must preserve those differences rather than pretending every provider can safely perform every role.

### Model and repository content

Treat prompts, model responses, tool output, repository files, generated patches, and provider metadata as untrusted data. They can contain misleading instructions, malformed structures, or adversarial content.

### Run evidence

Provenance, reports, diffs, and diagnostics are security-relevant evidence. They should describe what actually ran, not what the configuration merely intended to run.

## Security invariants

### Explicit role capability

A provider/backend must be rejected before dispatch when it cannot safely satisfy the selected role.

### No silent privilege substitution

Fallback must be explicit, bounded, role-scoped, and observable. A missing actor must never silently become a more privileged or different actor.

### Fail closed

Malformed provider output, unsupported capabilities, missing runtimes, invalid session state, or unsafe permission boundaries should stop the operation rather than guess.

### Least privilege

Read-only roles should not receive write or command-execution capability merely because another backend supports it.

### Secret minimization

Secrets belong in provider-owned state or explicit secret references. They must not be committed, echoed into logs, embedded in provenance, or copied into prompts without a documented requirement.

### Local management boundary

The dashboard and local control surfaces should remain bound to loopback by default. Network exposure should be an explicit operator decision with an appropriate authentication and transport-security review.

## Threats to consider

Changes should consider at least:

- prompt injection from repository content;
- command injection in subprocess construction;
- path traversal and symlink/junction escape;
- malicious or malformed provider responses;
- secret leakage through stderr/stdout or generated artifacts;
- fallback that changes the effective actor or permission level;
- stale or cross-profile session reuse;
- unsafe environment-variable propagation;
- tampering with provenance or review evidence;
- dependency and GitHub Actions supply-chain risk;
- local dashboard exposure or cross-site request attacks.

## Review expectations

Security-sensitive changes should include tests for the failure path, not only the success path.

When relevant, tests should prove:

- the expected actor and backend were selected;
- unsupported role/provider pairs are rejected before runtime;
- fallback use is recorded;
- secrets are absent from observable output;
- path constraints reject escapes;
- read-only roles cannot write;
- failure does not silently change actors.

## Vulnerability reporting

Follow [../SECURITY.md](../SECURITY.md) for private vulnerability reporting.
