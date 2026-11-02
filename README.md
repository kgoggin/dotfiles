# Dotfiles

## Repository disk audit

Install the read-only audit command (requires Git and Python 3.9 or newer):

```sh
install -m 755 code-disk-audit "$HOME/.local/bin/code-disk-audit"
```

Run it from any directory:

```sh
code-disk-audit
code-disk-audit ~/Code/zendesk
code-disk-audit ~/Code --output ~/disk-audits/after-cleanup
```

The default scan starts at `~/Code`. It discovers nested repositories, groups
checkouts by their shared Git directory, and follows all registered worktrees,
including checkouts outside the scan root. Nested clones inside those worktrees
are also discovered, along with their own registered worktrees. Existing bare
repositories and separate shared Git directories are included. Missing registered
worktrees have no disk usage to measure; they remain visible in the JSON inventory.

Each run writes `disk-audit.html`, `disk-audit.md`, `disk-audit.csv`, and
`disk-audit.json` to a new directory under `~/disk-audits/TIMESTAMP`, or to the
explicit `--output` directory. HTML has expandable repository rows showing each
checkout. Reports are sorted by total size and split into Zendesk and other repos;
Zendesk means repositories beneath the scan root's `zendesk` directory, or beneath
an enclosing directory named `zendesk` when scanning only part of that tree.
Reports include main checkout and linked worktree totals, `node_modules`, shared
Git metadata, individual checkout sizes, largest directories, and space outside
repositories. `node_modules` and Git metadata are subsets of the totals.

The scan measures allocated blocks, counts hard-linked files once across the audit,
and does not follow directory symlinks. Nested repos and checkouts are attributed
separately to avoid double counting. APFS clone sharing is not visible, so the
figures are not guaranteed reclaimable space. This is a live scan rather than a
filesystem snapshot; changing files can affect the totals. A full scan can take
several minutes. Progress goes to stderr; use `--quiet` to hide it. Files and Git
refs are only read; reports are the only files written. Scan errors produce
partial reports and exit status 1; Ctrl-C cancels with status 130.

## Disk cleanup

`disk-cleanup` reports what can be reclaimed and runs only the actions you
name. It uses `git-cleanup` from the same directory for worktrees, so install
both (Python 3.9 or newer):

```sh
install -m 755 disk-cleanup git-cleanup "$HOME/.local/bin/"
install -m 644 git-cleanup-select.py "$HOME/.local/bin/"
mkdir -p "$HOME/.claude/skills"
ln -sfn "$PWD/skills/disk-cleanup" "$HOME/.claude/skills/disk-cleanup"
```

The last command installs the `disk-cleanup` agent skill. With it, an agent
can produce the report, recommend what to remove, and run the cleanup only for
IDs you approve.

```sh
disk-cleanup report                      # every category; takes a few minutes
disk-cleanup report --only caches,docker # seconds
disk-cleanup clean wt-2 nm-1             # list what would run
disk-cleanup clean wt-2 nm-1 --yes       # run it
```

`report` saves its results to `~/.cache/disk-cleanup/report.json`; `clean`
accepts IDs from that report for one day. Every option has a risk level:
`safe` (regenerated on demand), `review` (check the note first), or `info`
(report only; there is no cleanup action).

- **Caches:** npm and Yarn 1 caches, `pnpm store prune`, and stores from
  older pnpm versions. It also offers older Playwright, Puppeteer, Cypress, and
  TypeScript builds, always keeping the newest of each, plus leftover Codex
  runtime installs that are more than a day old.
- **Docker:** `docker system prune -a -f`. Volumes are kept. The size estimate
  needs Docker Desktop running.
- **Worktrees:** `git cleanup --dry-run --json` runs in each repository under
  `~/Code` that has linked worktrees, including checkouts registered from
  `~/.t3/worktrees` and `~/conductor/workspaces`. Each eligible worktree is
  measured. `clean` applies the approved worktrees with `git cleanup --apply`,
  which repeats every eligibility and safety check.
- **Orphaned worktree directories:** directories in `~/.t3/worktrees/*/*` or
  `~/conductor/workspaces/*/*` that are not registered Git checkouts.
- **Idle node_modules:** `node_modules` directories within two levels of a
  checkout root, offered when the checkout's `HEAD` reflog has not changed in 30
  days. Change the threshold with `--stale-days`.
- **Report only:** `~/Movies`, protected folders (measured only when the
  terminal has Full Disk Access), Git metadata larger than 2 GiB, and APFS local
  snapshots.

Sizes are allocated space; hard links and APFS clones can make actual savings
smaller. `clean` rechecks orphaned directories and idle `node_modules` before
removing them, and reports the change in free space afterwards.

## Git cleanup

Install the worktree-aware cleanup script from this directory:

```sh
mkdir -p "$HOME/.local/bin"
install -m 755 git-cleanup "$HOME/.local/bin/git-cleanup"
install -m 644 git-cleanup-select.py "$HOME/.local/bin/git-cleanup-select.py"
git config --global alias.cleanup '!"$HOME/.local/bin/git-cleanup"'
```

Run `git cleanup` from any worktree. It prunes stale `origin` refs and
worktree registrations, fetches `origin/main`, and checks against that
frozen main commit. It does not switch branches or reset local `main`.
If main cannot be refreshed, cleanup stops before removing worktrees or
branches.

Cleanup scans the full inventory before opening a terminal checklist. Every
worktree is shown, including protected and ineligible checkouts with their
retention reasons. Eligible merged branches without worktrees are listed as
branch-only cleanup actions. Nothing is selected initially.

- Move with Up/Down or `j`/`k`; Page Up/Down and Home/End are also supported.
- Press Space to toggle an eligible entry. Each row shows whether its branch
  will be retained or deleted; the detail area shows its path and reason.
- Press `a` to select all eligible entries, or clear them if all are selected.
- Press Enter to review the entire batch, then `y` to apply it. `n` or Escape
  returns to the checklist. `q` cancels; Escape also cancels from the checklist.

The checklist uses Python 3's standard-library curses module; no additional
packages are needed. In a non-interactive terminal, cleanup prints the same
inventory and accepts numbered selections such as `1,3-5` or `all`, followed
by one batch confirmation. Blank input or EOF cancels. All selection happens
before any worktree or branch deletion. Removal then runs sequentially with
progress and a completion summary, without further prompts.

Cleanup uses these eligibility rules:

- **Idle worktree:** the branch tip's committer timestamp is at least seven
  days old. Cleanup **always retains its branch** when removing that checkout,
  even when the branch is merged. Commit author dates, directory modification
  times, and worktree creation dates are not used.
  Open PRs do not prevent idle checkout removal; their branches remain.
- **Merged branch:** ancestry proves the branch tip is in `origin/main`, or
  a merged GitHub PR targeting main has the exact same head and its merge
  commit is in main. A recent merged worktree can be selected for removal
  along with its branch. Merged branches without worktrees are deleted only when
  explicitly selected in the batch.
- **Merged changes with a different local tip:** if the complete branch
  patch can be reverse-applied to main, cleanup offers the checkout for
  review and removal, but retains the branch. This also uses a merged PR
  targeting main whose merge commit is in main. It catches some rebased or
  squash-merged changes that ancestry and exact commit checks miss.
- **Other recent worktrees:** cleanup reports an open PR, closed unmerged
  PR, unmatched branch, unconfirmed local changes, or unavailable PR check
  and keeps the checkout.

For GitHub repositories, the authenticated `gh` CLI queries PRs for each
branch needing a merge check, paging through all results. Fork PRs are not
used as merge evidence. Without `gh`, or if a lookup fails, Git ancestry
and idle checkout removal remain available.

No worktrees or branches are removed unless the batch is confirmed. Main
and current worktrees, `main` and `develop` branches, detached worktrees, and
branches checked out in multiple worktrees are protected and cannot be
selected. A worktree containing another registered worktree is also kept;
clean up its children first and rerun cleanup if desired.

Dirty or locked worktrees are kept and cause a nonzero exit status. Removal
never uses `--force`. As with ordinary `git worktree remove`, **ignored files
are removed**, including installed dependencies, build output, and ignored
scratch files. Review any ignored local material you want to keep before
confirming the batch. Branch tips, checkout identity, and nested worktrees
are checked again before each removal. A changed branch or a new nested
worktree prevents that action; Git also refuses a newly dirty or locked
checkout. Other selected actions continue after a failure. Branch deletion
uses `-D` only after the frozen main commit or an exact merged PR proves the tip merged,
so it is independent of the invoking checkout and branch upstream settings.

Preview the same decisions without prompting or removing worktrees,
branches, or refs:

```sh
git cleanup --dry-run
```

Add `--json` to a dry run to print the inventory as JSON on stdout (path,
branch, tip, mode, and reason for each entry), with progress on stderr. To
apply pre-approved actions without the checklist, pass a file whose lines are
`MODE TIP BRANCH`, using the mode and tip from that inventory:

```sh
git cleanup --apply approved.txt
```

Cleanup recomputes the inventory first. It skips any approved line whose
branch tip or mode no longer matches, or whose entry is now kept, and exits
nonzero. Everything else follows the confirmed-batch path above, including
rechecking each entry immediately before removal.

A dry run checks the current server main SHA and may download missing Git
objects. It does not update local refs or `FETCH_HEAD`. Content checks use
a temporary index and do not alter any checkout's files or index.

On a case-insensitive filesystem, remote refs differing only by case (such
as `Rishabh/...` and `rishabh/...`) can collide while Git locks them for
pruning. If normal pruning fails, cleanup checks the server and removes
case-colliding stale remote refs in separate transactions, then retries
normal pruning. It preserves the entire group if any case variant exists
on the server, verifies each ref's expected commit before deletion, and
never removes existing lock files. This repair supports the standard
`origin` fetch mapping; custom mappings and negative refspecs are left to Git.
Dry runs never perform this repair.

If pruning still fails, cleanup reports the failure and continues with
local cleanup after main is refreshed. Its exit status remains nonzero to
signal that pruning was incomplete. Git may print `[pruned]` lines even after
a pruning error; look for the error earlier in the output.

Run the integration checks with `python3 -m unittest discover -s tests`.
