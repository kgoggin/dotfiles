# Dotfiles

## Git cleanup

Install the worktree-aware cleanup script from this directory:

```sh
mkdir -p "$HOME/.local/bin"
install -m 755 git-cleanup "$HOME/.local/bin/git-cleanup"
git config --global alias.cleanup '!"$HOME/.local/bin/git-cleanup"'
```

Run `git cleanup` from any worktree. It prunes stale `origin` refs and
worktree registrations, fetches `origin/main`, and checks against that
frozen main commit. It does not switch branches or reset local `main`.
If main cannot be refreshed, cleanup stops before removing worktrees or
branches.

Cleanup prints a reason for each worktree candidate or retained worktree:

- **Idle worktree:** the branch tip's committer timestamp is at least seven
  days old. Cleanup asks before removing the worktree and **always retains
  its branch**, even when the branch is merged. Commit author dates,
  directory modification times, and worktree creation dates are not used.
  Open PRs do not prevent idle checkout removal; their branches remain.
- **Merged branch:** ancestry proves the branch tip is in `origin/main`, or
  a merged GitHub PR targeting main has the exact same head and its merge
  commit is in main. A recent merged worktree is offered for removal along
  with its branch. Merged branches without worktrees are deleted directly.
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

Removal always asks for confirmation. `y` approves the displayed action;
`n`, empty input, or end of input keeps the worktree and branch. Main and
current worktrees, `main` and `develop` branches, and detached worktrees are
protected. A worktree containing another registered worktree is kept;
clean up its children first and rerun cleanup if desired.

Dirty or locked worktrees are kept and cause a nonzero exit status. Removal
never uses `--force`. As with ordinary `git worktree remove`, **ignored files
are removed**, including installed dependencies, build output, and ignored
scratch files. Review any ignored local material you want to keep before
approving removal. Branch tips are checked again after the prompt; a
concurrent branch update prevents removal. Branch deletion uses `-D` only
after the frozen main commit or an exact merged PR proves the tip merged,
so it is independent of the invoking checkout and branch upstream settings.

Preview the same decisions without prompting or removing worktrees,
branches, or refs:

```sh
git cleanup --dry-run
```

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
