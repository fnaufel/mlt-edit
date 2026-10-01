#!/usr/bin/env bash
set -euo pipefail

if (( $# != 1 )); then
    echo "usage: $0 ISSUE"
    exit 2
fi

issue=$1

prompt=$(cat <<EOF
Use the \$implement skill to implement GitHub issue #${issue}.

This issue is the only implementation task for this run.

Read the issue and its comments with gh. Follow AGENTS.md, CONTEXT.md,
the relevant ADRs, and the repository's configured issue-tracker workflow.

Do not implement any other ticket and do not implement parent issue #1.
Do not push commits and do not create a pull request.

If an unresolved blocker prevents this issue from being implemented,
make no speculative implementation and clearly report the blocker.

When the implementation is complete, make sure the \$implement workflow
has run its required tests and review and has committed the completed
ticket locally.
EOF
)

codex \
    --sandbox workspace-write \
    exec "$prompt"
