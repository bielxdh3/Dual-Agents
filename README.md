<div align="center">

# Dual Agents

### Role-based orchestration for independent AI coding agents.

**Local-first · Multi-provider · Role-based · Auditable**

[![CI](https://github.com/bielxdh3/Dual-Agents/actions/workflows/tests.yml/badge.svg)](https://github.com/bielxdh3/Dual-Agents/actions/workflows/tests.yml)
[![Status](https://img.shields.io/badge/status-active%20development-orange)](#project-status)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D4)](#requirements)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)](#requirements)
[![Providers](https://img.shields.io/badge/providers-Codex%20%C2%B7%20Gemini%20%C2%B7%20Claude%20%C2%B7%20API-6f42c1)](#providers-and-roles)

Dual Agents is a local control plane for assigning independently configured AI providers to specialized coding roles, with explicit routing, bounded fallback, structured provenance, and provider-owned authentication.

</div>

> [!IMPORTANT]
> Dual Agents is under active development. The configured-actor architecture, Codex execution, Antigravity/Gemini execution, Claude read-only review, Claude native-Windows file editing, role-scoped fallback, and the local dashboard are implemented and tested. Provider limitations remain explicit and fail closed rather than silently changing actors or privileges.

## Why Dual Agents

<table>
<tr>
<td width="50%" valign="top">

### 🧠 Separate responsibilities
Architect, Executor, Reviewer, and Orchestrator are roles, not hard-coded providers. The same provider profile can fill multiple compatible roles, or each role can use a different actor.

</td>
<td width="50%" valign="top">

### 🔌 Use the provider that fits
Codex, Antigravity/Gemini, Claude Code, and OpenAI-compatible profiles live behind provider-aware capability checks instead of one universal execution path.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🛡️ Fail closed
Unavailable runtimes, unsupported roles, unsafe permission boundaries, malformed results, and invalid session state stop the run instead of silently falling back to a different agent.

</td>
<td width="50%" valign="top">

### 🔎 Keep the evidence
Runs preserve configured and actual actors, provider/backend details, session identity, result artifacts, Git state, and sanitized diagnostics so delegation remains inspectable.

</td>
</tr>
</table>

## The idea at a glance

\`\`\`text
                         ┌──────────────────────┐
                         │      User / task     │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │  Dual Agents control │
                         │  profiles · roles    │
                         │  policy · provenance │
                         └──────────┬───────────┘
                                    │
               ┌────────────────────┼────────────────────┐
               │                    │                    │
        ┌──────▼──────┐      ┌──────▼──────┐      ┌──────▼──────┐
        │  Architect  │      │   Executor  │      │   Reviewer  │
        │ configured  │      │ configured  │      │ configured  │
        │ actor       │      │ actor       │      │ actor       │
        └──────┬──────┘      └──────┬──────┘      └──────┬──────┘
               │                    │                    │
               └────────────────────┼────────────────────┘
                                    │
                         ┌──────────▼───────────┐
                         │ result · diff ·      │
                         │ report · provenance  │
                         └──────────────────────┘

Optional fallback is role-scoped, explicit, and limited to eligible profiles.
\`\`\`

A **profile is not a role**. A profile owns provider metadata and authentication state; role assignment decides what that profile is allowed to do in the orchestration flow.

## Providers and roles

| Provider | Orchestrator | Architect | Executor | Reviewer | Notes |
|---|:---:|:---:|:---:|:---:|---|
| **Codex** | ✅ | ✅ | ✅ | ✅ | Isolated by \`CODEX_HOME\`; native Windows and App Server transports are supported where configured. |
| **Antigravity / Gemini** | — | — | ✅ | — | Headless executor through the installed \`agy\` runtime. Current runtime does not advertise isolated multi-account state roots. |
| **Anthropic Claude** | — | ✅ | ⚠️ | ✅ | Isolated with \`CLAUDE_CONFIG_DIR\`. Native-Windows Executor is file-edit-only; command-running Executor is blocked. |
| **OpenAI-compatible API** | ✅ | ✅ | — | ✅ | BYOK through \`env:VARIABLE\`; no local tool/workspace execution is claimed. |

Provider support is capability-driven. A configured profile is rejected before dispatch when its runtime cannot safely satisfy the selected role.

## What it can do

- assign independent provider profiles to **Orchestrator, Architect, Executor, and Reviewer** roles;
- reassign roles without re-authenticating an existing profile;
- use **Codex** as an Architect, Reviewer, Orchestrator, or Executor;
- use **Antigravity/Gemini** as a structured headless Executor;
- use **Claude Code** for bounded Architect/Reviewer work and file-edit-only execution on native Windows;
- use declared **OpenAI-compatible API** profiles for non-tool roles;
- configure provider-aware models, reasoning/effort, and supported service tiers;
- keep Codex state isolated by \`CODEX_HOME\` and Claude state by \`CLAUDE_CONFIG_DIR\`;
- preserve exact provider sessions where the adapter supports safe continuation;
- enable optional, deterministic **role-scoped fallback** without arbitrary actor substitution;
- manage profiles and roles from a loopback-only dashboard;
- expose live Executor activity from real run journals instead of simulated UI state;
- persist structured run evidence and sanitized provenance.

## Project status

**Current state: active development on \`main\`.**

<details>
<summary><strong>Implementation matrix</strong></summary>

| Area | Current state |
|---|---|
| Configured actor routing | Implemented |
| Role/profile separation | Implemented |
| Codex provider | Implemented |
| Antigravity/Gemini provider | Implemented for Executor |
| Claude Code provider | Implemented; read-only roles and native-Windows file-only Executor bounded |
| OpenAI-compatible API profiles | Implemented for declared non-tool roles |
| Role-scoped fallback | Implemented; disabled by default |
| Dashboard profile management | Implemented |
| Provider-aware model/effort controls | Implemented |
| Live Executor telemetry | Implemented |
| Native Windows persistent Codex TUI | Implemented |
| WSL Claude command-running Executor | Deferred |
| Claude two-account live isolation | Not yet live-proven |
| Cross-platform runtime parity | Not a current guarantee |

</details>

The project intentionally distinguishes **implemented**, **live-proven**, and **unsupported** behavior instead of treating provider capabilities as interchangeable.

## Technology

| Layer | Technology |
|---|---|
| Core | Python 3.11+ |
| Configuration | TOML |
| Dashboard | Python loopback HTTP server + embedded HTML/JS |
| Native Codex terminal | Node.js + \`node-pty\` / Windows ConPTY |
| Codex transport | native TUI and App Server |
| Gemini transport | \`agy\` stream-json |
| Claude transport | Claude Code headless JSON |
| API transport | OpenAI-compatible \`/chat/completions\` |
| Validation | Python \`unittest\` + Node syntax/audit checks |
| Primary platform | Windows |

## Requirements

Core:

- **Windows 10/11**
- **Python 3.11+**
- **Git**

Optional provider/runtime requirements depend on what you use:

- **Codex CLI** for Codex profiles;
- **Node.js 22+** for the persistent ConPTY host;
- **Antigravity \`agy\`** for Gemini Executor profiles;
- **Claude Code** for Anthropic Claude profiles;
- an HTTPS endpoint plus environment-variable secret reference for OpenAI-compatible API profiles.

You do not need every provider installed to use Dual Agents.

## Quick start

### 1. Clone and install

\`\`\`powershell
git clone https://github.com/bielxdh3/Dual-Agents.git
cd Dual-Agents

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
\`\`\`

Install the Node dependency only when using the persistent native Codex terminal host:

\`\`\`powershell
npm ci
\`\`\`

### 2. Create local configuration

\`\`\`powershell
Copy-Item config.example.toml config.toml
notepad config.toml
\`\`\`

\`config.toml\` is local configuration. Provider secrets should not be written into it.

### 3. Check the environment

\`\`\`powershell
dual-codex doctor
dual-codex status
\`\`\`

The package still exposes the historical \`dual-codex\` CLI name; the product and repository are now **Dual Agents**.

### 4. Open the dashboard

\`\`\`powershell
dual-codex dashboard
\`\`\`

The dashboard binds to \`127.0.0.1\` only and opens the local profile/role control plane.

### 5. Create profiles and assign roles

Profiles can be created from the dashboard or CLI. Authentication stays provider-owned: Codex uses its profile \`CODEX_HOME\`, Claude Code uses its isolated state root, Gemini uses the installed \`agy\` runtime, and API profiles reference an environment variable.

Example role layout:

\`\`\`text
Orchestrator → Codex primary
Architect    → Codex primary
Executor     → Codex secondary
Reviewer     → Claude
\`\`\`

This is only an example. Roles are independent from profile names and providers.

### 6. Run or delegate

\`\`\`powershell
dual-codex run task.md
\`\`\`

For explicit delegation:

\`\`\`powershell
dual-codex delegate --request-file request.json --result-file result.json
\`\`\`

The Windows launcher can also be used directly:

\`\`\`powershell
.\scripts\dual-codex.ps1 --config .\config.toml delegate --request-file .\request.json --result-file .\result.json
\`\`\`

See [CLI reference](docs/CLI.md) for schemas and advanced options.

## Dashboard

The local dashboard is the normal control surface for:

- creating, renaming, enabling, disabling, and removing profile metadata;
- checking provider authentication/runtime status;
- selecting provider-supported models and reasoning/effort levels;
- assigning primary roles;
- configuring fallback eligibility separately from primary roles;
- viewing rate-limit or usage data when the provider exposes it;
- watching real Executor activity through the **EXECUTOR LIVE** view.

Provider controls are capability-aware. For example, Claude exposes documented model aliases such as Sonnet, Opus, Haiku, and Fable; effort controls are enabled only when that model family supports them.

Settings apply to future turns and do not silently rewrite an already-running provider session.

## Fallback

Automatic fallback is **off by default**.

When enabled, fallback remains constrained by:

\`\`\`text
global fallback switch
+ profile enabled
+ role listed in fallback_roles
+ provider supports that role
+ runtime preflight passes
+ failure is fallback-eligible
\`\`\`

Only one fallback actor is attempted. Semantic task failures and security denials do not become excuses to route work to another provider.

## Local-first and security boundary

Dual Agents is designed around explicit local trust boundaries:

- provider credentials remain owned by their native runtime or environment-variable secret reference;
- Codex profiles are isolated by \`CODEX_HOME\`;
- Claude profiles are isolated with \`CLAUDE_CONFIG_DIR\`;
- the dashboard listens on loopback and validates Host/Origin;
- repository/workspace identity is bound before provider dispatch;
- role-specific tool permissions are enforced before the model turn;
- unavailable or unsafe provider states fail closed;
- automatic configured-actor fallback is opt-in and role-scoped;
- structured provenance records the configured and actual actor used;
- diagnostics are sanitized before persistence;
- internal chain-of-thought is not exposed as dashboard telemetry.

> [!NOTE]
> Claude Code on native Windows does not provide the OS command sandbox required for a command-running Executor. Dual Agents therefore keeps native-Windows Claude execution file-edit-only. WSL2 command execution is separate future scope.

## Persistent Codex terminals

Dual Agents can host persistent Codex sessions through Windows ConPTY:

\`\`\`powershell
dual-codex terminal start primary --role architect --attach
dual-codex terminal start secondary --role executor --attach
dual-codex terminal list
\`\`\`

Interactive attach connects to the managed session instead of spawning another Codex process. Strict reuse verifies account, role, repository, process identity, and session readiness before adoption.

Advanced terminal behavior is documented in [CLI.md](docs/CLI.md) and [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Validation

Run the Python suite:

\`\`\`powershell
python -m unittest discover -s tests -v
python -m compileall -q src tests
\`\`\`

Validate the Node host:

\`\`\`powershell
npm ci
node --check scripts/pty-host.js
npm audit --omit=dev
\`\`\`

GitHub Actions runs the test matrix on Python 3.11, 3.12, and 3.13 plus the Node validation job.

## Repository map

\`\`\`text
Dual-Agents/
├── src/                     Python orchestration and provider adapters
├── tests/                   unit and regression tests
├── docs/                    CLI, app integration and troubleshooting
├── scripts/                 launcher and native terminal host
├── schemas/                 typed request/result contracts
├── prompts/                 prompt assets used by the orchestration flow
├── .github/                 CI configuration
├── config.example.toml      example local configuration
├── task.example.md          example task
├── CHANGELOG.md             user-visible change history
├── AGENTS.md                repository agent instructions
└── README.md
\`\`\`

## Documentation

- [CLI reference](docs/CLI.md)
- [Codex App integration](docs/APP-INTEGRATION.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Changelog](CHANGELOG.md)
- [Repository agent instructions](AGENTS.md)

Deep runtime details, schemas, migration behavior, terminal internals, and failure recovery belong in the focused documentation above rather than turning this README into an implementation log.

## Current limitations

- Windows is the primary supported runtime target.
- Antigravity/Gemini is currently an Executor-only provider.
- The installed \`agy\` runtime does not currently provide verified isolated multi-account state roots.
- Native-Windows Claude command execution remains intentionally unavailable; Claude Executor is file-edit-only.
- Claude multi-account isolation uses separate state roots, but two independently authenticated accounts have not been live-validated together.
- OpenAI-compatible API profiles do not provide local workspace/tool execution.
- Some legacy \`dual-codex\` naming remains in the CLI and package metadata.

## Project philosophy

Dual Agents does not assume that one model should plan, implement, review, authenticate, and publish everything by itself.

The control plane owns **who is assigned**, **what that actor is allowed to do**, **how the runtime is bound**, and **what evidence is preserved**. Providers remain replaceable; the routing and trust boundary stay explicit.
