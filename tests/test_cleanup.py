import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
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
        self.git("config", "alias.cleanup", "!" + shlex.quote(str(SCRIPT)))

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

    def cleanup(self, answer="", cwd=None):
        return self.git("cleanup", input=answer, check=False, cwd=cwd)

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
        result = self.cleanup("maybe\ny\n")
        self.assertIn("Please answer y or n", result.stdout)
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


if __name__ == "__main__":
    unittest.main()
