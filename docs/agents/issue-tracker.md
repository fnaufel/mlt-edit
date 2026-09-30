# Issue tracker: GitHub

Issues and specs live in GitHub Issues for `fnaufel/mlt-edit`. Run `gh` from this repo so it uses the GitHub remote.

## Operations

- Create an issue with `gh issue create --title "..." --body-file <path>`.
- Read an issue and its comments with `gh issue view <number> --comments`.
- List open issues with `gh issue list --state open --json number,title,body,labels,comments`.
- Comment with `gh issue comment <number> --body-file <path>`.
- Add or remove labels with `gh issue edit <number> --add-label <label>` or `--remove-label <label>`.
- Close with `gh issue close <number>`.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Skill conventions

When a skill says “publish to the issue tracker,” create a GitHub issue. When it says “fetch the relevant ticket,” read the referenced GitHub issue and its comments.

For a wayfinding map, use one issue labelled `wayfinder:map`. Link child tickets as GitHub sub-issues when available, or use a task list in the map issue. Record blockers with GitHub issue dependencies when available, or a `Blocked by: #...` line. Claim a ticket by assigning it to yourself; resolve it by recording the answer and closing it.
