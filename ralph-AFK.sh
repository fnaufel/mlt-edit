#!/usr/bin/env bash
set -euo pipefail

tickets=(2 3 4 5 6 9 7 8)

if [[ -n $(git status --porcelain) ]]; then
    echo "Working tree is not clean. Refusing to start."
    exit 1
fi

for issue in "${tickets[@]}"; do
    state=$(gh issue view "$issue" --json state --jq '.state')

    if [[ "$state" != "OPEN" ]]; then
        echo "Skipping #$issue: $state"
        continue
    fi

    echo
    echo "===== Ralph: issue #$issue ====="
    echo

    prompt=$(cat <<EOF
Use the \$implement skill to implement GitHub issue #${issue}.

Work on exactly this one ticket.

Before changing code:
- read issue #${issue} and its comments with gh;
- obey AGENTS.md, CONTEXT.md, the relevant ADRs, and docs/specs.md;
- verify that every Blocked-by issue is closed.

Do not implement parent issue #1 or any other ticket.
Do not push.
Do not create a pull request.

If the ticket is blocked, stop without making unrelated changes.

Complete the normal \$implement workflow, including its tests, code review,
and local commit.

Only after all acceptance criteria are satisfied and the implementation
has been committed, close GitHub issue #${issue} with a concise comment
giving the commit SHA.
EOF
)

    codex \
        --ask-for-approval never \
        --sandbox workspace-write \
        exec "$prompt"

    if [[ -n $(git status --porcelain) ]]; then
        echo "Issue #$issue left a dirty working tree. Stopping."
        exit 1
    fi

    state=$(gh issue view "$issue" --json state --jq '.state')

    if [[ "$state" != "CLOSED" ]]; then
        echo "Issue #$issue is still open. Stopping."
        exit 1
    fi
done
