# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Development Guidelines

Before writing, editing, fixing, refactoring, or reviewing any code in this
repo, always invoke the `dev-guidelines` skill first and apply it for the
rest of the task. This applies to every coding task regardless of how the
request is phrased — do not wait for the user to name the skill explicitly.

If the `dev-guidelines` skill is unavailable in a given environment, fall
back to its core rules: modular and lean code (no premature abstraction),
type hints and `pathlib` for Python, pinned dependency versions, no
hardcoded secrets, parameterized SQL only, unit test stubs alongside
non-trivial code, and Conventional Commits for commit messages.
