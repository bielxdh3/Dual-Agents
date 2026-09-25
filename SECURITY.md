# Security Policy

Security reports are taken seriously, especially because Dual Agents can coordinate provider runtimes that read repositories, write files, and execute commands depending on the configured role and backend.

## Supported versions

Security fixes target the current `main` branch and the most recent published release or tag when one exists. Older revisions may receive fixes only when practical.

## Reporting a vulnerability

Do **not** open a public issue for an undisclosed vulnerability.

Preferred reporting path:

1. Use GitHub's private vulnerability reporting / Security Advisory flow for this repository when it is available.
2. If private reporting is not available in the repository UI, contact the maintainer through a private contact method listed on the maintainer's GitHub profile.

Include enough information to reproduce and assess the issue:

- affected version, tag, or commit;
- operating system;
- provider/backend involved;
- configuration relevant to the issue, with secrets removed;
- reproduction steps or a minimal proof of concept;
- expected and observed behavior;
- security impact;
- logs only after removing tokens, credentials, local paths, and private repository content.

Please do not include live credentials, provider authentication files, access tokens, private keys, or personal data in a report.

## High-priority areas

Reports are especially useful for issues involving:

- authentication or provider-state isolation;
- command execution outside the intended boundary;
- workspace writes by a role that should be read-only;
- path traversal or repository-boundary escape;
- secret disclosure in logs, artifacts, prompts, or provenance;
- unsafe fallback to a different provider or privilege level;
- dashboard exposure beyond the intended local boundary;
- injection across prompts, subprocesses, configuration, or provider adapters;
- tampering with run evidence or provenance;
- dependency or supply-chain compromise.

## Disclosure

Please allow reasonable time to investigate and prepare a fix before public disclosure. Once a fix is available, the project may publish a security advisory with affected versions, remediation steps, and credit when appropriate.

## Security design

The repository's intended trust boundaries and security invariants are documented in [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).
