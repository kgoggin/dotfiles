import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "disk-cleanup"


class DiskCleanupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="disk-cleanup-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        # Only Git and Python are visible, so real package managers are never run.
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        (bin_dir / "git").symlink_to(shutil.which("git"))
        (bin_dir / "python3").symlink_to(sys.executable)
        self.env = dict(os.environ, HOME=str(self.home), GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                        PATH=os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]))
        self.code = self.home / "Code"
        self.code.mkdir()

    def git(self, path, *args):
        return subprocess.run(["git", "-C", str(path), *args], env=self.env, text=True,
                              capture_output=True, check=True).stdout

    def repo(self, name="repo"):
        origin = self.home / "origins" / name
        path = self.code / name
        self.git(self.home, "init", "-q", "--bare", str(origin))
        self.git(self.home, "init", "-q", "-b", "main", str(path))
        for key, value in [("user.name", "Test"), ("user.email", "test@example.com"),
                           ("commit.gpgsign", "false"), ("core.hooksPath", os.devnull)]:
            self.git(path, "config", key, value)
        (path / ".gitignore").write_text("node_modules/\n")
        self.git(path, "add", ".gitignore")
        self.git(path, "commit", "-qm", "Initial")
        self.git(path, "remote", "add", "origin", str(origin))
        self.git(path, "push", "-q", "-u", "origin", "main")
        return path

    def fill(self, path, size=16384):
        path.mkdir(parents=True, exist_ok=True)
        (path / "data").write_bytes(b"x" * size)
        return path

    def run_script(self, *args, check=True):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env,
                                text=True, capture_output=True, check=False)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def report(self, *categories):
        result = self.run_script("report", "--json", "--only", ",".join(categories))
        return {o["id"]: o for o in json.loads(result.stdout)["options"]}

    def test_worktree_is_removed_only_after_yes(self):
        repo = self.repo()
        path = self.home / ".t3" / "worktrees" / "repo" / "topic"
        self.git(repo, "worktree", "add", "-q", "-b", "topic", str(path))
        self.fill(path / "node_modules")
        options = self.report("worktrees")
        self.assertEqual(options["wt-1"]["mode"], "delete")
        self.assertEqual(Path(options["wt-1"]["path"]).resolve(), path)
        listed = self.run_script("clean", "wt-1")
        self.assertIn("Nothing changed", listed.stdout)
        self.assertTrue(path.exists())
        self.run_script("clean", "wt-1", "--yes")
        self.assertFalse(path.exists())
        self.assertNotIn("topic", self.git(repo, "branch", "--list", "topic"))

    def test_worktree_changed_after_report_is_kept(self):
        repo = self.repo()
        path = self.code / "topic"
        self.git(repo, "worktree", "add", "-q", "-b", "topic", str(path))
        self.report("worktrees")
        self.git(path, "commit", "-q", "--allow-empty", "-m", "New work")
        result = self.run_script("clean", "wt-1", "--yes", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(path.exists())

    def test_orphaned_directory_is_offered_but_registered_worktree_is_not(self):
        repo = self.repo()
        root = self.home / ".t3" / "worktrees" / "repo"
        registered = root / "registered"
        self.git(repo, "worktree", "add", "-q", "-b", "topic", str(registered))
        orphan = self.fill(root / "orphan")
        (orphan / ".git").write_text("gitdir: /missing/worktrees/orphan\n")
        options = self.report("orphans")
        self.assertEqual([o["path"] for o in options.values()], [str(orphan)])
        self.run_script("clean", "orphan-1", "--yes")
        self.assertFalse(orphan.exists())
        self.assertTrue(registered.exists())

    def test_only_idle_checkouts_offer_node_modules(self):
        idle = self.repo("idle")
        busy = self.repo("busy")
        for repo in (idle, busy):
            self.fill(repo / "node_modules")
            self.fill(repo / "packages" / "app" / "node_modules")
        old = time.time() - 40 * 86400
        os.utime(idle / ".git" / "logs" / "HEAD", (old, old))
        options = self.report("node-modules")
        self.assertEqual(list(options), ["nm-1"])
        self.assertEqual(options["nm-1"]["checkout"], str(idle))
        self.assertEqual(len(options["nm-1"]["paths"]), 2)
        self.run_script("clean", "nm-1", "--yes")
        self.assertFalse((idle / "node_modules").exists())
        self.assertFalse((idle / "packages" / "app" / "node_modules").exists())
        self.assertTrue((idle / ".gitignore").exists())
        self.assertTrue((busy / "node_modules").exists())

    def test_browser_caches_keep_the_newest_build_of_each_browser(self):
        cache = self.home / "Library" / "Caches" / "ms-playwright"
        builds = {name: self.fill(cache / name) for name in
                  ["chromium-1194", "chromium-1248", "firefox-1495"]}
        old = time.time() - 86400
        os.utime(builds["chromium-1194"], (old, old))
        options = self.report("caches")
        self.assertEqual(options["playwright-old"]["paths"], [str(builds["chromium-1194"])])
        self.run_script("clean", "playwright-old", "--yes")
        self.assertFalse(builds["chromium-1194"].exists())
        self.assertTrue(builds["chromium-1248"].exists())
        self.assertTrue(builds["firefox-1495"].exists())

    def test_clean_rejects_unknown_report_only_and_old_reports(self):
        result = self.run_script("clean", "wt-1", check=False)
        self.assertIn("run `disk-cleanup report` first", result.stderr)
        self.fill(self.home / "Movies")
        options = self.report("info")
        info = next(i for i, o in options.items() if o["title"] == "~/Movies")
        result = self.run_script("clean", info, "--yes", check=False)
        self.assertIn("Report-only", result.stderr)
        self.assertTrue((self.home / "Movies").exists())
        result = self.run_script("clean", "nope", check=False)
        self.assertIn("Unknown IDs: nope", result.stderr)
        state = self.home / ".cache" / "disk-cleanup" / "report.json"
        report = json.loads(state.read_text())
        report["created"] -= 2 * 86400
        state.write_text(json.dumps(report))
        result = self.run_script("clean", info, check=False)
        self.assertIn("over a day old", result.stderr)


if __name__ == "__main__":
    unittest.main()
