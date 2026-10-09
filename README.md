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

The main and current worktrees are protected. Worktree removal never uses
`--force`, so Git refuses dirty or locked worktrees and their branches are
kept. Unmerged branches are left alone.

Run the integration checks with `python3 -m unittest discover -s tests`.
