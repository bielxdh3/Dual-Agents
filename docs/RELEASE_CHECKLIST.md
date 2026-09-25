# Release Checklist

Use this checklist before publishing a Dual Agents release.

## Source state

- [ ] Release commit is on the intended branch.
- [ ] Working tree is clean.
- [ ] Version numbers are internally consistent.
- [ ] CHANGELOG describes user-visible changes.
- [ ] README and CLI documentation match the release behavior.
- [ ] No local run artifacts, credentials, provider state, or private repository data are included.

## Validation

- [ ] Python unit tests pass.
- [ ] Python sources compile.
- [ ] JSON schemas parse.
- [ ] Node host syntax check passes.
- [ ] Node dependency audit is reviewed.
- [ ] Git whitespace check passes.
- [ ] CodeQL findings are reviewed.
- [ ] Dependency Review has no blocking finding for the release change set.

## Security boundaries

- [ ] Provider/role capability claims match the implementation.
- [ ] Unsupported combinations still fail closed.
- [ ] Fallback remains explicit and observable.
- [ ] Authentication state remains isolated.
- [ ] Logs and provenance contain no secrets.
- [ ] Workspace/path changes were reviewed for boundary escape.
- [ ] Dashboard/network behavior remains consistent with documented exposure.

## Packaging and provenance

- [ ] Release artifacts come from the reviewed commit.
- [ ] Generated artifacts can be traced back to the release commit.
- [ ] Checksums are recorded when binary or archive artifacts are published.
- [ ] Release notes identify breaking configuration or compatibility changes.

## Publication

- [ ] Create the release tag from the reviewed commit.
- [ ] Publish release notes.
- [ ] Verify links and downloadable artifacts.
- [ ] Re-check the published tag/commit association.
- [ ] Record any known limitations or deferred fixes.

Do not mark a release as validated when a required check was skipped. Record the skipped check and reason explicitly.
