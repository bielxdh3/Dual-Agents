# Governance

Dual Agents currently uses a maintainer-led governance model.

## Maintainer responsibilities

Maintainers are responsible for:

- repository direction and scope;
- merge and release decisions;
- security response;
- review standards;
- compatibility policy;
- repository settings and automation.

## Contributions

Contributors propose changes through issues and pull requests. A contribution can be technically sound and still be declined when it conflicts with project scope, trust boundaries, maintainability, or the current roadmap.

## Decision making

Routine changes are decided through pull-request review.

Changes that affect authentication, provider isolation, command execution, workspace permissions, fallback behavior, provenance, or public compatibility should favor explicit behavior, testable evidence, and fail-closed defaults.

## Releases

A release should only be published after the relevant checks in [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) are satisfied.

## Security

Embargoed vulnerability handling follows [SECURITY.md](SECURITY.md). Security-sensitive implementation decisions should also remain consistent with [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

## Governance changes

This file can evolve as the contributor base grows. Changes to governance should be proposed in a pull request and explained in the PR description.
