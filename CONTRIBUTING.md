# Contributing to Ouroboros

External contributions are welcome. This file is only a contributor-facing
routing guide; the project rules live in the canonical docs.

## Canonical Docs

- `README.md` - install, source setup, tests, build notes, and user-facing overview.
- `BIBLE.md` - constitutional principles and design priority.
- `docs/ARCHITECTURE.md` - current structure, runtime/data map, endpoints, and release model.
- `docs/DEVELOPMENT.md` - engineering conventions, module boundaries, and testing guidance.
- `docs/CHECKLISTS.md` - review checklist single source of truth.

If this file and a canonical doc disagree, the canonical doc wins. Update the
canonical doc instead of copying detailed rules here.

## Before Opening a PR

1. Follow the `README.md` source setup and test instructions.
2. Read the relevant part of `docs/DEVELOPMENT.md` before changing runtime,
   tools, platform code, provider calls, web UI, skills, prompts, or governance docs.
3. Keep the PR focused on one coherent change.
4. Add or update tests for changed behavior.
5. In the PR body, explain why the change is needed, link the issue when there
   is one, and include the exact commands or checks you ran.

## Review Notes

External PRs are reviewed by maintainers with local checks and any configured
automation. Ouroboros also has an internal self-modification commit gate;
contributors do not need to run that gate for a normal fork PR, but
`docs/CHECKLISTS.md` is the best summary of what reviewers will care about.

## Sensitive Areas

Ask before making broad changes to constitutional docs, runtime prompts, safety
policy, frozen contracts, release/build plumbing, managed git behavior, or
runtime state under `data/`. These areas have stronger review expectations
because they affect identity, safety, compatibility, or release provenance.

Do not include secrets, local settings, generated runtime state, logs, caches,
or build artifacts in a PR.

## License

By contributing, you agree that your contribution can be released under the
repository license.
