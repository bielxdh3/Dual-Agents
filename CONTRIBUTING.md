# Contributing to Dual Agents

Thanks for contributing to Dual Agents.

Dual Agents is a local-first control plane that can invoke AI providers against real repositories. Changes that affect provider routing, permissions, authentication boundaries, command execution, provenance, or workspace writes therefore deserve the same care as security-sensitive infrastructure.

## Before you start

- Read [README.md](README.md) for the current product scope.
- Read [AGENTS.md](AGENTS.md) before using an AI coding agent in this repository.
- Check [SECURITY.md](SECURITY.md) before reporting a vulnerability.
- Keep changes focused. Large unrelated refactors are harder to review and audit.

## Development environment

Primary development and CI coverage is Windows.

Requirements:

- Python 3.11 or newer;
- Node.js 22 or newer;
- npm;
- Git;
- provider runtimes only when the change actually needs integration testing.

Recommended setup:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e .
npm ci
```

Do not place provider credentials, authentication files, private repositories, or generated run artifacts in the repository.

## Validation

Run the checks relevant to your change before opening a pull request:

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python -m compileall -q src tests
node --check scripts/pty-host.js
npm audit --omit=dev
git diff --check
```

If your change affects provider dispatch or role behavior, add or update tests that prove the configured actor, backend, fallback state, and failure mode.

## Security-sensitive changes

Changes in these areas require extra scrutiny:

- provider authentication or state directories;
- role-to-provider routing;
- command execution or workspace writes;
- fallback behavior;
- path handling;
- secrets or environment variables;
- run artifacts, logs, or provenance;
- local dashboard exposure;
- subprocess invocation.

Preserve the project's fail-closed behavior. Do not silently substitute a different provider, role, permission level, or execution path.

Treat repository content, prompts, model output, provider responses, imported configuration, and subprocess output as untrusted input.

## Pull requests

A good pull request should:

- explain the problem and the intended behavior;
- keep the diff as small as reasonably possible;
- include tests for behavior changes;
- document new configuration or user-facing behavior;
- identify security or compatibility implications;
- avoid committing generated secrets, local state, or run output.

Use the repository pull request template and include the commands you ran.

## Commit messages

Prefer concise imperative or conventional-style messages, for example:

```text
fix: reject unsupported reviewer backend
feat: persist provider provenance
docs: document release validation
test: cover role-scoped fallback
```

## AI-assisted contributions

AI-assisted work is welcome, but the contributor remains responsible for the submitted code.

Before submitting:

- inspect the actual diff;
- run the relevant tests;
- verify that generated code did not introduce secrets or unsafe permissions;
- verify provider and role claims against the implementation;
- do not present generated output as reviewed evidence unless you actually reviewed it.

## Documentation

Update documentation when a change modifies commands, configuration, supported providers, trust boundaries, failure behavior, or public interfaces.

## Review

Maintainers may request smaller changes, additional tests, clearer evidence, or a follow-up security review before merge.
