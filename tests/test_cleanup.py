import json
import os
from pathlib import Path
import shlex
import select
import shutil
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "git-cleanup"


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="git-cleanup-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        self.git("init", "--bare", str(self.root / "origin"), cwd=self.root)
        self.git("init", "-b", "main", str(self.repo), cwd=self.root)
        self.git("config", "user.name", "Cleanup Test")
        self.git("config", "user.email", "cleanup@example.com")
        self.git("config", "core.hooksPath", os.devnull)
        self.git("config", "commit.gpgsign", "false")
        self.git("commit", "--allow-empty", "-m", "Initial")
        self.git("remote", "add", "origin", str(self.root / "origin"))
        self.git("push", "-u", "origin", "main")
        # An installed git-cleanup on PATH can take precedence over its alias.
        self.git("config", "alias.cleanup-test", "!" + shlex.quote(str(SCRIPT)))

    def git(self, *args, cwd=None, check=True, input=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo,
                              env=self.env, input=input, text=True,
                              capture_output=True, check=check)

    def worktree(self, branch="topic", name="linked worktree"):
        path = self.root / name
        self.git("worktree", "add", "-b", branch, str(path))
        return path

    def has_branch(self, branch):
        return self.git("show-ref", "--verify", "--quiet",
                        "refs/heads/" + branch, check=False).returncode == 0

    def cleanup(self, answer="y\n", cwd=None):
        # Existing behavior tests choose the same actions through the batch
        # fallback. No deletion occurs until the final y below.
        answers = answer.splitlines()
        if not answers or not any(a.lower() in ('y', 'yes') for a in answers):
            batch = "\n"
        elif len(answers) == 1:
            batch = "all\ny\n"
        else:
            choices = [str(i + 1) for i, a in enumerate(answers)
                       if a.lower() in ('y', 'yes')]
            batch = ",".join(choices) + "\ny\n"
        return self.git("cleanup-test", input=batch, check=False, cwd=cwd)

    def squash_worktree(self):
        path = self.worktree()
        (path / "feature.txt").write_text("Merged feature\n")
        self.git("add", "feature.txt", cwd=path)
        self.git("commit", "-m", "Feature", cwd=path)
        tip = self.git("rev-parse", "topic").stdout.strip()
        self.git("merge", "--squash", "topic")
        self.git("commit", "-m", "Squash feature")
        merged = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("push", "origin", "main")
        self.assertNotEqual(self.git("merge-base", "--is-ancestor", "topic", "HEAD",
                                     check=False).returncode, 0)
        self.git("config", "remote.origin.url", "https://github.com/example/repo.git")
        self.git("config", "url." + str(self.root / "origin") + ".insteadOf",
                 "https://github.com/example/repo.git")
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        gh = fake_bin / "gh"
        gh.write_text('#!/bin/sh\nprintf "%s" "$CLEANUP_TEST_PRS"\n'
                      'exit "${CLEANUP_TEST_GH_STATUS:-0}"\n')
        gh.chmod(0o755)
        self.env["PATH"] = str(fake_bin) + os.pathsep + self.env["PATH"]
        self.env["CLEANUP_TEST_PRS"] = f"topic\t{tip}\t{merged}\t123\tMERGED\tmain\n"
        return path

    def test_squash_merged_worktree_is_offered_and_removed(self):
        path = self.squash_worktree()
        result = self.cleanup("y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("merged PR #123", result.stdout)
        self.assertFalse(path.exists())
        self.assertFalse(self.has_branch("topic"))

    def test_squash_merged_worktree_no_keeps_both(self):
        path = self.squash_worktree()
        result = self.cleanup("n\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Remove worktree", result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_dirty_squash_worktree_is_preserved(self):
        path = self.squash_worktree()
        (path / "feature.txt").write_text("New uncommitted work\n")
        result = self.cleanup("y\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((path / "feature.txt").read_text(), "New uncommitted work\n")
        self.assertTrue(self.has_branch("topic"))

    def test_commits_after_squash_merge_are_preserved(self):
        path = self.squash_worktree()
        (path / "new.txt").write_text("Not in main\n")
        self.git("add", "new.txt", cwd=path)
        self.git("commit", "-m", "New work", cwd=path)
        result = self.cleanup("y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Remove worktree", result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_squash_merge_on_remote_main_is_offered_despite_stale_local_main(self):
        path = self.squash_worktree()
        self.git("reset", "--hard", "HEAD^")
        result = self.cleanup("y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Remove worktree", result.stdout)
        self.assertFalse(path.exists())
        self.assertFalse(self.has_branch("topic"))

    def test_github_failure_keeps_squash_worktree(self):
        path = self.squash_worktree()
        self.env["CLEANUP_TEST_GH_STATUS"] = "1"
        result = self.cleanup("y\n")
        self.assertIn("Could not check GitHub", result.stderr)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_dry_run_keeps_branches_and_worktrees(self):
        path = self.squash_worktree()
        self.git("branch", "ordinary")
        result = self.git("cleanup-test", "--dry-run", input="y\n", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Would remove worktree", result.stdout)
        self.assertIn("Would delete merged branch ordinary", result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))
        self.assertTrue(self.has_branch("ordinary"))

    def test_failed_remote_prune_still_prompts_for_worktrees(self):
        path = self.squash_worktree()
        fake_bin = self.root / "bin"
        real_git = shutil.which("git")
        git = fake_bin / "git"
        git.write_text('#!/bin/sh\n'
                       'if [ "$1" = remote ] && [ "$2" = prune ]; then\n'
                       '  echo "error: could not delete references" >&2\n'
                       '  echo " * [pruned] origin/example"\n'
                       '  exit 1\n'
                       'fi\n'
                       'exec ' + shlex.quote(real_git) + ' "$@"\n')
        git.chmod(0o755)
        self.env["GIT_EXEC_PATH"] = str(fake_bin)
        result = self.cleanup("n\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Remote ref pruning failed; continuing", result.stderr)
        self.assertIn("Remove worktree", result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def case_colliding_refs(self, names=("Rishabh/topic", "rishabh/topic")):
        oid = self.git("rev-parse", "HEAD").stdout.strip()
        # Packed refs can represent names that cannot coexist as loose files
        # on a case-insensitive filesystem, reproducing the real repository.
        packed = self.repo / ".git" / "packed-refs"
        packed.write_text("# pack-refs with: peeled fully-peeled sorted\n" +
                          "".join(f"{oid} refs/remotes/origin/{name}\n"
                                  for name in sorted(names)))
        return ["refs/remotes/origin/" + name for name in names]

    def has_ref(self, ref):
        return self.git("show-ref", "--verify", "--quiet", ref,
                        check=False).returncode == 0

    def test_case_colliding_stale_refs_are_pruned(self):
        refs = self.case_colliding_refs()
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        for ref in refs:
            self.assertFalse(self.has_ref(ref))
        self.assertTrue(self.has_ref("refs/remotes/origin/main"))
        probe = self.root / "case-probe"
        probe.touch()
        if (self.root / "CASE-PROBE").exists():
            self.assertIn("Pruning stale refs with case-colliding names", result.stdout)

    def test_dry_run_preserves_case_colliding_refs(self):
        refs = self.case_colliding_refs()
        result = self.git("cleanup-test", "--dry-run", input="", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        for ref in refs:
            self.assertTrue(self.has_ref(ref))

    def test_live_case_variant_is_preserved(self):
        self.git("push", "origin", "main:refs/heads/RISHABH/topic")
        refs = self.case_colliding_refs(("RISHABH/topic", "Rishabh/topic", "rishabh/topic"))
        self.cleanup()
        self.assertTrue(self.has_ref(refs[0]))

    def test_case_collision_repair_respects_existing_locks(self):
        refs = self.case_colliding_refs()
        lock = self.repo / ".git/refs/remotes/origin/Rishabh/topic.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("Existing lock")
        result = self.cleanup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(lock.read_text(), "Existing lock")
        self.assertTrue(self.has_ref(refs[0]))

    def test_yes_removes_worktree_then_branch(self):
        path = self.worktree(name='linked "worktree"\nwith newline')
        result = self.cleanup("y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assertFalse(self.has_branch("topic"))

    def test_no_blank_and_eof_keep_both(self):
        path = self.worktree()
        for answer in ("n\n", "\n", ""):
            with self.subTest(answer=answer):
                self.assertEqual(self.cleanup(answer).returncode, 0)
                self.assertTrue(path.exists())
                self.assertTrue(self.has_branch("topic"))

    def test_invalid_answer_reprompts(self):
        path = self.worktree()
        result = self.git("cleanup-test", input="invalid\nall\ny\n", check=False)
        self.assertIn("Invalid selection", result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assertFalse(self.has_branch("topic"))

    def test_separate_answers_for_multiple_worktrees(self):
        first = self.worktree("a", "first")
        second = self.worktree("b", "second")
        result = self.cleanup("n\ny\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(first.exists())
        self.assertTrue(self.has_branch("a"))
        self.assertFalse(second.exists())
        self.assertFalse(self.has_branch("b"))

    def test_dirty_worktree_is_preserved(self):
        path = self.worktree()
        (path / "untracked.txt").write_text("Keep this work")
        result = self.cleanup("y\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((path / "untracked.txt").read_text(), "Keep this work")
        self.assertTrue(self.has_branch("topic"))

    def test_locked_worktree_is_preserved(self):
        path = self.worktree()
        self.git("worktree", "lock", str(path))
        self.assertNotEqual(self.cleanup("y\n").returncode, 0)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_unmerged_worktree_is_not_offered(self):
        path = self.worktree()
        self.git("commit", "--allow-empty", "-m", "Unmerged", cwd=path)
        result = self.cleanup("y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Remove worktree", result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_main_current_and_develop_are_protected(self):
        self.git("switch", "-c", "primary-topic")
        current = self.worktree("current-topic", "current")
        develop = self.worktree("develop", "develop")
        result = self.cleanup("y\ny\ny\n", cwd=current)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Remove worktree", result.stdout)
        for branch in ("main", "develop", "primary-topic", "current-topic"):
            self.assertTrue(self.has_branch(branch))
        for path in (self.repo, current, develop):
            self.assertTrue(path.exists())

    def test_plain_merged_branch_and_remote_refs_are_pruned(self):
        self.git("branch", "topic")
        self.git("tag", "topic")  # Ambiguous short ref names must still work.
        self.git("push", "origin", "refs/heads/topic:refs/heads/topic")
        self.git("update-ref", "-d", "refs/heads/topic", cwd=self.root / "origin")
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.has_branch("topic"))
        self.assertNotEqual(self.git("show-ref", "--verify", "--quiet",
                                    "refs/remotes/origin/topic", check=False).returncode, 0)

    def test_missing_worktree_registration_is_pruned(self):
        path = self.worktree()
        shutil.rmtree(path)
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Remove worktree", result.stdout)
        self.assertFalse(self.has_branch("topic"))
        self.assertNotIn(str(path), self.git("worktree", "list", "--porcelain").stdout)


    def dated_commit(self, path, days, author_only=False):
        timestamp = int(time.time()) - days * 86400
        saved = self.env.copy()
        try:
            self.env['GIT_AUTHOR_DATE'] = f'{timestamp} +0000'
            if not author_only:
                self.env['GIT_COMMITTER_DATE'] = f'{timestamp} +0000'
            self.git('commit', '--allow-empty', '-m', f'{days} day old work', cwd=path)
        finally:
            self.env = saved
        return self.git('rev-parse', 'HEAD', cwd=path).stdout.strip()

    def test_idle_unmerged_worktree_removal_retains_exact_branch_tip(self):
        path = self.worktree()
        tip = self.dated_commit(path, 8)
        result = self.cleanup('y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('idle: last commit 8 days ago', result.stdout)
        self.assertIn('retain branch "topic"', result.stdout)
        self.assertFalse(path.exists())
        self.assertEqual(self.git('rev-parse', 'topic').stdout.strip(), tip)

    def test_idle_merged_worktree_also_retains_branch(self):
        path = self.worktree()
        tip = self.dated_commit(path, 8)
        self.git('merge', '--ff-only', 'topic')
        self.git('push', 'origin', 'main')
        result = self.cleanup('y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assertEqual(self.git('rev-parse', 'topic').stdout.strip(), tip)

    def test_six_day_old_unmerged_worktree_is_kept(self):
        path = self.worktree()
        self.dated_commit(path, 6)
        result = self.cleanup('y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('last commit is less than 7 days old', result.stdout)
        self.assertNotIn('Remove worktree', result.stdout)
        self.assertTrue(path.exists())

    def test_idle_uses_committer_date_instead_of_author_date(self):
        path = self.worktree()
        self.dated_commit(path, 30, author_only=True)
        result = self.cleanup('y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Remove worktree', result.stdout)
        self.assertTrue(path.exists())

    def test_exactly_seven_days_old_is_idle(self):
        path = self.worktree()
        self.dated_commit(path, 7)
        timestamp = int(self.git('show', '-s', '--format=%ct', 'topic').stdout)
        fake_bin = self.root / 'bin'
        fake_bin.mkdir()
        date = fake_bin / 'date'
        date.write_text(f'#!/bin/sh\nprintf "%s\\n" {timestamp + 7 * 86400}\n')
        date.chmod(0o755)
        self.env['PATH'] = str(fake_bin) + os.pathsep + self.env['PATH']
        result = self.cleanup('y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('idle: last commit 7 days ago', result.stdout)
        self.assertFalse(path.exists())
        self.assertTrue(self.has_branch('topic'))

    def test_idle_decline_and_eof_keep_worktree_and_branch(self):
        path = self.worktree()
        self.dated_commit(path, 8)
        for answer in ('n\n', '\n', ''):
            result = self.cleanup(answer)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(path.exists())
            self.assertTrue(self.has_branch('topic'))

    def test_dirty_idle_worktree_is_kept_without_prompting(self):
        path = self.worktree()
        self.dated_commit(path, 8)
        (path / 'new.txt').write_text('Uncommitted work\n')
        result = self.cleanup('y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('uncommitted or untracked changes', result.stdout)
        self.assertNotIn('Remove worktree', result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch('topic'))

    def test_idle_removal_can_discard_ignored_dependencies_but_keeps_branch(self):
        path = self.worktree()
        self.dated_commit(path, 8)
        self.git('config', 'core.excludesFile', str(self.root / 'ignores'))
        (self.root / 'ignores').write_text('node_modules/\n')
        dependencies = path / 'node_modules'
        dependencies.mkdir()
        (dependencies / 'installed.txt').write_text('Reinstallable\n')
        result = self.cleanup('y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assertTrue(self.has_branch('topic'))

    def test_locked_idle_worktree_is_kept(self):
        path = self.worktree()
        self.dated_commit(path, 8)
        self.git('worktree', 'lock', str(path))
        result = self.cleanup('y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('worktree is locked', result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch('topic'))

    def test_detached_worktree_is_kept_even_when_old(self):
        self.dated_commit(self.repo, 8)
        self.git('push', 'origin', 'main')
        path = self.root / 'detached'
        self.git('worktree', 'add', '--detach', str(path))
        result = self.cleanup('y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('detached HEAD; no branch to retain', result.stdout)
        self.assertTrue(path.exists())

    def test_parent_worktree_is_kept_when_it_contains_another_worktree(self):
        parent = self.worktree('parent', 'parent')
        self.dated_commit(parent, 8)
        child = parent / 'nested'
        self.git('worktree', 'add', '-b', 'child', str(child), 'parent')
        result = self.cleanup('n\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('contains another registered worktree', result.stdout)
        self.assertTrue(parent.exists())
        self.assertTrue(child.exists())
        self.assertTrue(self.has_branch('parent'))

    def test_invoking_feature_head_is_not_treated_as_main(self):
        invoker = self.worktree('feature', 'invoker')
        self.git('commit', '--allow-empty', '-m', 'Feature only', cwd=invoker)
        child = self.root / 'child'
        self.git('worktree', 'add', '-b', 'child', str(child), 'feature')
        result = self.cleanup('y\n', cwd=invoker)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Remove worktree', result.stdout)
        self.assertTrue(child.exists())
        self.assertTrue(self.has_branch('child'))

    def test_fresh_origin_main_is_used_from_a_stale_feature_checkout(self):
        invoker = self.worktree('feature', 'invoker')
        path = self.worktree()
        (path / 'landed.txt').write_text('Landed\n')
        self.git('add', 'landed.txt', cwd=path)
        self.git('commit', '-m', 'Landed change', cwd=path)
        self.git('merge', '--ff-only', 'topic')
        self.git('push', 'origin', 'main')
        self.git('reset', '--hard', 'main^')
        local_main = self.git('rev-parse', 'main').stdout
        result = self.cleanup('y\n', cwd=invoker)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assertFalse(self.has_branch('topic'))
        self.assertEqual(self.git('rev-parse', 'main').stdout, local_main)
        self.assertTrue(invoker.exists())

    def test_main_refresh_failure_prevents_deletion(self):
        path = self.worktree()
        self.git('config', 'remote.origin.url', str(self.root / 'missing'))
        result = self.cleanup('y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Remove worktree', result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch('topic'))

    def test_dry_run_fetches_missing_main_objects_without_changing_refs(self):
        self.worktree()
        publisher = self.root / 'publisher'
        self.git('clone', '-b', 'main', str(self.root / 'origin'), str(publisher))
        self.git('config', 'user.name', 'Cleanup Test', cwd=publisher)
        self.git('config', 'user.email', 'cleanup@example.com', cwd=publisher)
        self.git('config', 'commit.gpgsign', 'false', cwd=publisher)
        self.git('commit', '--allow-empty', '-m', 'New remote main', cwd=publisher)
        self.git('push', 'origin', 'main', cwd=publisher)
        remote_tip = self.git('rev-parse', 'HEAD', cwd=publisher).stdout.strip()
        self.assertNotEqual(self.git('cat-file', '-e', remote_tip, check=False).returncode, 0)
        refs = self.git('for-each-ref', '--format=%(refname) %(objectname)').stdout
        fetch_head = self.repo / '.git' / 'FETCH_HEAD'
        fetch_before = fetch_head.read_bytes() if fetch_head.exists() else None
        result = self.git('cleanup-test', '--dry-run', check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(remote_tip[:12], result.stdout)
        self.assertEqual(self.git('for-each-ref', '--format=%(refname) %(objectname)').stdout, refs)
        self.assertEqual(fetch_head.read_bytes() if fetch_head.exists() else None, fetch_before)
        self.assertEqual(self.git('cat-file', '-e', remote_tip, check=False).returncode, 0)

    def test_dry_run_idle_candidate_retains_everything(self):
        path = self.worktree()
        tip = self.dated_commit(path, 8)
        result = self.git('cleanup-test', '--dry-run', check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('retain branch "topic"', result.stdout)
        self.assertTrue(path.exists())
        self.assertEqual(self.git('rev-parse', 'topic').stdout.strip(), tip)

    def test_content_equivalent_different_tip_is_reviewed_and_branch_retained(self):
        path = self.squash_worktree()
        self.git('commit', '--amend', '--no-edit', '--reset-author', cwd=path)
        # Avoid depending on whether the amend occurred in the same second.
        self.git('commit', '--allow-empty', '-m', 'Rewritten local tip', cwd=path)
        tip = self.git('rev-parse', 'topic').stdout.strip()
        result = self.cleanup('y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('review: merged PR #123 has a different tip', result.stdout)
        self.assertIn('complete branch patch is present', result.stdout)
        self.assertFalse(path.exists())
        self.assertEqual(self.git('rev-parse', 'topic').stdout.strip(), tip)

    def test_open_pr_is_reported_even_without_a_merge_commit(self):
        path = self.squash_worktree()
        self.git('reset', '--hard', 'HEAD^')
        self.git('push', '--force', 'origin', 'main')
        tip = self.git('rev-parse', 'topic').stdout.strip()
        self.env['CLEANUP_TEST_PRS'] = f'topic\t{tip}\t-\t123\tOPEN\tmain\n'
        result = self.cleanup('y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('open PR #123', result.stdout)
        self.assertTrue(path.exists())

    def test_merged_pr_must_have_landed_in_main(self):
        path = self.squash_worktree()
        tip = self.git('rev-parse', 'topic').stdout.strip()
        self.env['CLEANUP_TEST_PRS'] = f'topic\t{tip}\t{tip}\t123\tMERGED\tmain\n'
        result = self.cleanup('y\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Remove worktree', result.stdout)
        self.assertTrue(path.exists())
        self.assertIn('local changes are not confirmed', result.stdout)

    def test_pr_lookup_is_paginated_and_scoped_to_branch(self):
        path = self.squash_worktree()
        gh = self.root / 'bin' / 'gh'
        gh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CLEANUP_TEST_CALLS"\n'
                      'printf "%s" "$CLEANUP_TEST_PRS"\n')
        self.env['CLEANUP_TEST_CALLS'] = str(self.root / 'calls')
        result = self.cleanup('n\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / 'calls').read_text().splitlines()
        self.assertIn('--paginate', calls)
        self.assertIn('head=example:topic', calls)
        self.assertIn('state=all', calls)
        self.assertTrue(path.exists())

    def test_branch_tip_change_during_cleanup_prevents_removal(self):
        path = self.squash_worktree()
        gh = self.root / 'bin' / 'gh'
        gh.write_text('#!/bin/sh\ngit -C "$CLEANUP_TEST_PATH" commit --allow-empty '
                      '-m "Concurrent work" >/dev/null\nprintf "%s" "$CLEANUP_TEST_PRS"\n')
        self.env['CLEANUP_TEST_PATH'] = str(path)
        result = self.cleanup('y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('branch changed since the cleanup check', result.stderr)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch('topic'))



    def scanned_batch(self):
        process = subprocess.Popen(['git', 'cleanup-test'], cwd=self.repo,
                                   env=self.env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        output = b''
        deadline = time.monotonic() + 15
        while b'Select numbers/ranges or all' not in output:
            if time.monotonic() > deadline or process.poll() is not None:
                self.fail(f'Batch selector did not appear: {output!r}')
            if select.select([process.stdout], [], [], 0.1)[0]:
                output += os.read(process.stdout.fileno(), 65536)
        return process, output

    def test_batch_waits_for_all_selections_before_removing_anything(self):
        first = self.worktree('a', 'first')
        second = self.worktree('b', 'second')
        process, output = self.scanned_batch()
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertIn(b'first', output)
        self.assertIn(b'second', output)
        remaining, errors = process.communicate(b'all\ny\n', timeout=15)
        self.assertEqual(process.returncode, 0, errors.decode())
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertIn(b'[1/2]', remaining)
        self.assertIn(b'[2/2]', remaining)
        self.assertIn(b'2 actions completed', remaining)

    def test_batch_cancel_keeps_worktrees_and_branch_only_candidates(self):
        path = self.worktree()
        self.git('branch', 'orphan')
        result = self.git('cleanup-test', input='all\nn\n\n', check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Cleanup cancelled', result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch('topic'))
        self.assertTrue(self.has_branch('orphan'))

    def test_batch_eof_before_final_confirmation_does_not_remove_anything(self):
        path = self.worktree()
        result = self.git('cleanup-test', input='all\n', check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch('topic'))

    def test_batch_skips_newly_dirty_checkout_and_continues_with_next(self):
        first = self.worktree('a', 'first')
        second = self.worktree('b', 'second')
        process, _ = self.scanned_batch()
        (first / 'new-work.txt').write_text('Created while checklist was open\n')
        output, errors = process.communicate(b'all\ny\n', timeout=15)
        self.assertNotEqual(process.returncode, 0)
        self.assertTrue(first.exists())
        self.assertTrue(self.has_branch('a'))
        self.assertFalse(second.exists())
        self.assertFalse(self.has_branch('b'))
        self.assertIn(b'1 actions completed; 1 skipped or failed', output)

    def test_batch_commit_while_selecting_preserves_idle_worktree(self):
        path = self.worktree()
        self.dated_commit(path, 8)
        process, _ = self.scanned_batch()
        self.git('commit', '--allow-empty', '-m', 'New work during selection', cwd=path)
        tip = self.git('rev-parse', 'topic').stdout
        output, errors = process.communicate(b'all\ny\n', timeout=15)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'branch changed since the cleanup check', errors)
        self.assertTrue(path.exists())
        self.assertEqual(self.git('rev-parse', 'topic').stdout, tip)

    def test_batch_new_nested_worktree_prevents_parent_removal(self):
        parent = self.worktree()
        self.dated_commit(parent, 8)
        process, _ = self.scanned_batch()
        child = parent / 'new-nested-worktree'
        self.git('worktree', 'add', '-b', 'child', str(child), 'topic')
        output, errors = process.communicate(b'all\ny\n', timeout=15)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn(b'now contains another worktree', errors)
        self.assertTrue(parent.exists())
        self.assertTrue(child.exists())
        self.assertTrue(self.has_branch('topic'))

    def test_batch_branch_only_update_during_selection_is_preserved(self):
        self.git('branch', 'orphan')
        process, _ = self.scanned_batch()
        tip = self.git('commit-tree', 'HEAD^{tree}', '-p', 'HEAD', '-m', 'New work').stdout.strip()
        self.git('update-ref', 'refs/heads/orphan', tip)
        output, errors = process.communicate(b'all\ny\n', timeout=15)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(self.git('rev-parse', 'orphan').stdout.strip(), tip)
        self.assertIn(b'branch changed since the cleanup check', errors)

    def test_both_worktrees_are_visible_when_a_branch_is_checked_out_twice(self):
        first = self.worktree()
        second = self.root / 'second copy'
        self.git('worktree', 'add', '--force', str(second), 'topic')
        result = self.git('cleanup-test', input='\n', check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(first), result.stdout)
        self.assertIn(str(second), result.stdout)
        self.assertIn('branch is checked out in multiple worktrees', result.stdout)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def inventory(self, status=0):
        result = self.git("cleanup-test", "--dry-run", "--json", check=False)
        self.assertEqual(result.returncode, status, result.stderr)
        return json.loads(result.stdout)

    def apply(self, *entries):
        approved = self.root / "approved"
        approved.write_text("".join(
            f"{e['mode']} {e['tip']} {e['branch']}\n" for e in entries))
        return self.git("cleanup-test", "--apply", str(approved), check=False)

    def entry(self, inventory, branch):
        return next(e for e in inventory["entries"] if e["branch"] == branch)

    def test_dry_run_json_prints_only_the_inventory_on_stdout(self):
        path = self.worktree()
        inventory = self.inventory()
        self.assertEqual(Path(inventory["repository"]).resolve(), self.repo.resolve())
        entry = self.entry(inventory, "topic")
        self.assertEqual(entry["mode"], "delete")
        self.assertEqual(Path(entry["path"]).resolve(), path.resolve())
        self.assertEqual(self.entry(inventory, "main")["mode"], "keep")
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_json_requires_dry_run_and_apply_excludes_dry_run(self):
        for args in (["--json"], ["--dry-run", "--apply", "x"], ["--apply"]):
            result = self.git("cleanup-test", *args, check=False)
            self.assertEqual(result.returncode, 2, args)

    def test_apply_removes_only_approved_actions_without_prompting(self):
        topic = self.worktree()
        other = self.worktree("other", "other worktree")
        result = self.apply(self.entry(self.inventory(), "topic"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(topic.exists())
        self.assertFalse(self.has_branch("topic"))
        self.assertTrue(other.exists())
        self.assertTrue(self.has_branch("other"))

    def test_apply_retains_idle_branch(self):
        path = self.worktree()
        tip = self.dated_commit(path, 8)
        entry = self.entry(self.inventory(), "topic")
        self.assertEqual(entry["mode"], "retain")
        result = self.apply(entry)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assertEqual(self.git("rev-parse", "topic").stdout.strip(), tip)

    def test_apply_skips_action_whose_branch_moved_after_the_report(self):
        path = self.worktree()
        entry = self.entry(self.inventory(), "topic")
        self.git("commit", "--allow-empty", "-m", "New work", cwd=path)
        result = self.apply(entry)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no longer eligible", result.stderr)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_apply_skips_action_whose_mode_differs(self):
        path = self.worktree()
        self.dated_commit(path, 8)
        entry = dict(self.entry(self.inventory(), "topic"), mode="delete")
        result = self.apply(entry)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_apply_never_selects_kept_entries(self):
        path = self.worktree()
        (path / "scratch.txt").write_text("uncommitted\n")
        # Dirty checkouts make the dry run fail, but the inventory is still printed.
        entry = self.entry(self.inventory(status=1), "topic")
        self.assertEqual(entry["mode"], "keep")
        result = self.apply(entry)
        self.assertEqual(result.returncode, 1)
        self.assertTrue((path / "scratch.txt").exists())


if __name__ == "__main__":
    unittest.main()
