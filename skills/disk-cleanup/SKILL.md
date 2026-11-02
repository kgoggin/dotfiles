---
name: disk-cleanup
description: Report what is using disk space on this Mac and what can be reclaimed — package manager and browser caches, Docker, idle or merged Git worktrees (using git cleanup's rules), orphaned worktree directories, and idle node_modules — then run only the cleanup actions the user explicitly approves. Use when the user is low on disk space, asks what is taking up space, or asks to free space or clean up worktrees, caches, or node_modules.
---

# Disk cleanup

`disk-cleanup` is installed at `~/.local/bin/disk-cleanup` (source:
`~/Code/dotfiles/disk-cleanup`). It has two commands:

- `disk-cleanup report` measures cleanup options. It is read-only apart from
  saving the report to `~/.cache/disk-cleanup/report.json`.
- `disk-cleanup clean ID... --yes` runs the named options from that saved
  report. Without `--yes` it only lists what would run.

## 1. Report

Run the report with a long timeout; a full report takes several minutes
because it runs `git cleanup --dry-run` (network fetches and GitHub PR
lookups) in every repository that has linked worktrees:

```sh
disk-cleanup report --json
```

Use a 10-minute timeout. For a quick look, limit categories with
`--only caches,docker` (seconds) and run the rest afterwards. Categories:
`caches`, `docker`, `worktrees`, `orphans`, `node-modules`, `info`.

Each option has an `id`, `bytes`, `risk`, `title`, `action`, and `note`:

- `safe`: regenerated on demand (old browser builds, package caches, idle
  `node_modules`).
- `review`: worth a look first — worktree removal, orphaned directories,
  Docker prune, older pnpm stores, leftover runtime installs.
- `info`: report only (Movies, protected folders, large `.git`
  directories). There is no cleanup action for these.

## 2. Present it

Show free space, then a table grouped by category with ID, size, risk, and
what the action does, largest first within each group. Include the note for
every `review` item. Then recommend a selection, with the total it would free,
and explain any review items you excluded. Mention warnings (for example,
repositories where `git cleanup` failed) and `info` items worth acting on by
hand.

Worktree options follow `git cleanup`'s rules. Say whether each one retains
the branch (idle for 7+ days) or deletes it (proven merged). Removing a
worktree also deletes its ignored files, such as `node_modules` and build
output.

Sizes are allocated space. Hard links and APFS clones can make the real
savings smaller, especially for the pnpm store and `node_modules`.

## 3. Clean only with explicit approval

Never run `disk-cleanup clean ... --yes` unless the user has approved those
exact IDs in this conversation, after seeing the report. "Clean up whatever is
safe" counts as approval only for the specific IDs you then list back and the
user confirms. Approval does not carry over to later sessions, and a new report
renumbers IDs, so get approval again after rerunning it.

Once IDs are approved:

```sh
disk-cleanup clean wt-1 wt-3 npm-cache --yes
```

Before running, `clean` checks again that the actions are still valid:

- Worktrees go through `git cleanup --apply`. It skips any worktree whose
  branch moved, whose action changed, or that became dirty or locked.
- Orphaned directories must still be unregistered.
- `node_modules` must still be idle.

Report the skipped items and the change in free space from the command's
output. If anything was skipped or failed, say so with its reason. Do not
retry by other means: never `rm` a worktree, use `git worktree remove
--force`, or delete paths yourself.

Saved reports expire after a day. If `clean` says the report is missing or
stale, rerun the report and get approval again.
