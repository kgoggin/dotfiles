# Dotfiles

## Git cleanup

Install the worktree-aware cleanup script from this directory:

```sh
mkdir -p "$HOME/.local/bin"
install -m 755 git-cleanup "$HOME/.local/bin/git-cleanup"
git config --global alias.cleanup '!"$HOME/.local/bin/git-cleanup"'
```

Run `git cleanup` after checking out your base branch. It prunes remote refs
from `origin` and stale worktree registrations, then deletes local branches
merged into the current `HEAD`, excluding `main`, `develop`, and the current
branch. For a merged branch in a linked worktree, answer `y` to remove the
worktree and delete the branch, or `n` to keep both. Empty input or end of
input also keeps both.

Squash and rebase merges do not preserve the original branch's ancestry. For
GitHub repositories, cleanup also uses the authenticated `gh` CLI to check
the latest 1,000 merged PRs targeting the current branch. A linked worktree
is offered only if its branch exactly matches the PR's head commit and the
PR's merge commit is an ancestor of the current `HEAD`. Branches with newer
local commits, PRs merged into another base, and PRs from forks are skipped.
Without `gh` or when the lookup fails, cleanup uses Git ancestry alone.

If remote ref pruning fails, cleanup reports the failure and continues with
local branch and worktree cleanup. Its exit status remains nonzero to signal
that pruning was incomplete. Git may print `[pruned]` lines even after a
pruning error; look for the error earlier in the output. On a case-insensitive
filesystem, remote refs differing only by case (such as `Rishabh/...` and
`rishabh/...`) can collide while Git locks them for pruning. This does not
prevent checking merged branches or prompting for linked worktrees.

Preview candidates without removing worktrees, branches, or refs:

```sh
git cleanup --dry-run
```

The main and current worktrees are protected. Worktree removal never uses
`--force`, so Git refuses dirty or locked worktrees and their branches are
kept. Branch deletion uses `-D` only for a linked branch verified against a
merged PR, after its worktree is removed and its tip is checked again.
Unmerged branches are left alone.

Run the integration checks with `python3 -m unittest discover -s tests`.
