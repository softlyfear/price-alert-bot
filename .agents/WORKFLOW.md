# Repository synchronization

For authorized repository updates, run `python3 scripts/sync_upstream.py --json`.
The helper uses the current branch's configured upstream, preserves local work,
and does not run deployment, application processes, or database migrations.

## Approved upstream-rewrite exception

The user approved this narrow exception for selected server checkouts:
`agentSync.allowRewrittenUpstream=true` in local Git configuration allows the
shared helper to accept a rewritten upstream only when the checked-out branch
is still `main`, the tree is clean, no Git operation is active, and HEAD equals
the tracking OID captured before fetch. This equality establishes that the
checkout has no unpublished local commits relative to the previously known
upstream. The helper preserves old HEAD in a unique
`refs/agent-sync/backups/` reference before replacing the tracked tree. An expected-OID Git reference
transaction locks HEAD and the branch; an index lock protects the tree update.
This provides the intended preservation of `reset --keep` without its unchecked
HEAD-update race. Unsupported transaction features stop before checkout.

The opt-in is local and is not enabled by cloning these instructions. Other
checkouts use fast-forward updates until the user approves enabling it there.
Dirty files, ignored-file collisions, branch/HEAD races, or divergent local
commits stop the update. The helper does not stash, force-push, or replay local
commits. A blocked update does not advance the tracking reference, preserving
the evidence needed for a safe retry. Do not replace this protocol with a blind
hard reset, a forced pull, or an unconditional rebase.

Backup references remain local and are never included in the branch push.
Use them for inspection or a separately considered recovery; do not push the
old history back over the rewritten upstream.
