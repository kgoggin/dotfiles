import csv
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "code-disk-audit"
loader = importlib.machinery.SourceFileLoader("disk_audit", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
audit = importlib.util.module_from_spec(spec)
loader.exec_module(audit)


class DiskAuditTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.root = self.directory / "Code"
        self.root.mkdir()
        self.output = self.directory / "reports"
        self.output.mkdir()

    def git(self, path, *args):
        return subprocess.check_output(
            ["git", "-C", str(path), *args], stderr=subprocess.PIPE
        )

    def repo(self, path, separate=None, bare=False):
        path.mkdir(parents=True)
        args = ["init", "-q", "-b", "main"]
        if separate:
            args += ["--separate-git-dir", str(separate)]
        if bare:
            self.git(path, *args, "--bare")
        else:
            self.git(path, *args)
            (path / "file").write_bytes(b"x" * 8192)
            self.git(path, "add", "file")
            self.git(
                path,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "Initial commit",
            )
        return path

    def worktree(self, repo, path, branch):
        self.git(repo, "worktree", "add", "-qb", branch, str(path))
        return path

    def run_audit(self):
        result = audit.Audit(str(self.root), quiet=True).run()
        self.assertEqual(result["errors"], [])
        audit.write_reports(result, self.output)
        return result

    def test_groups_main_nested_and_external_worktrees_without_double_counting(self):
        main = self.repo(self.root / "zendesk" / "app")
        nested = self.worktree(main, main / ".worktrees" / "nested", "nested")
        external = self.worktree(main, self.directory / "external", "external")
        (nested / "node_modules").mkdir()
        (nested / "node_modules" / "dependency").write_bytes(b"d" * 16384)
        result = self.run_audit()
        self.assertEqual(len(result["groups"]), 1)
        repo = result["repositories"][0]
        self.assertEqual(repo["org"], "Zendesk")
        self.assertEqual(repo["worktree_count"], 2)
        checkouts = {c["path"]: c for c in repo["checkouts"]}
        self.assertEqual(
            checkouts[str(main)]["top"][".worktrees"],
            (main / ".worktrees").stat().st_blocks * 512,
        )
        self.assertGreater(checkouts[str(nested)]["dependencies_bytes"], 0)
        self.assertEqual(repo["bytes"], repo["main_bytes"] + repo["worktree_bytes"])
        self.assertEqual(
            repo["bytes"] + sum(result["other"].values()),
            result["base_bytes"] + result["external_bytes"],
        )
        self.assertGreater(result["external_bytes"], 0)
        self.assertIn(str(external), checkouts)

    def test_nested_external_clone_discovers_its_own_worktrees(self):
        main = self.repo(self.root / "zendesk" / "app")
        external = self.worktree(main, self.directory / "external", "external")
        child = self.repo(external / "nested-clone")
        distant = self.worktree(child, self.directory / "distant", "distant")
        result = self.run_audit()
        self.assertEqual(len(result["groups"]), 2)
        self.assertIn(str(distant), [c["path"] for c in result["checkouts"]])
        self.assertEqual({r["org"] for r in result["repositories"]}, {"Zendesk"})
        self.assertEqual(
            sum(r["bytes"] for r in result["repositories"])
            + sum(result["other"].values()),
            result["base_bytes"] + result["external_bytes"],
        )

    def test_hardlinks_counted_once_and_directory_symlinks_not_followed(self):
        main = self.repo(self.root / "app")
        payload = main / "payload"
        payload.write_bytes(b"z" * 32768)
        os.link(payload, main / "payload-copy")
        outside = self.directory / "unrelated"
        outside.mkdir()
        (outside / "ignored").write_bytes(b"z" * 65536)
        (main / "symlink").symlink_to(outside, target_is_directory=True)
        result = self.run_audit()
        top = result["checkouts"][0]["top"]
        self.assertEqual(
            top["payload"] + top["payload-copy"], payload.stat().st_blocks * 512
        )
        self.assertEqual(top["symlink"], (main / "symlink").lstat().st_blocks * 512)
        self.assertEqual(result["external_bytes"], 0)

    def test_shared_git_directory_outside_root_counted_once(self):
        common = self.directory / "shared.git"
        main = self.repo(self.root / "app", separate=common)
        self.worktree(main, self.directory / "external", "external")
        result = self.run_audit()
        repo = result["repositories"][0]
        self.assertGreater(repo["metadata_bytes"], 0)
        self.assertIn(str(common), result["scanned"])
        self.assertEqual(
            repo["bytes"] + sum(result["other"].values()),
            result["base_bytes"] + result["external_bytes"],
        )

    def test_shared_git_directory_inside_root_has_repository_attribution(self):
        # Metadata sorts before the checkout during discovery.
        common = self.root / "a-metadata"
        self.repo(self.root / "z-checkout", separate=common)
        result = self.run_audit()
        repo = result["repositories"][0]
        self.assertGreater(repo["metadata_bytes"], 0)
        self.assertNotIn("a-metadata", result["other"])
        self.assertEqual(
            repo["bytes"] + sum(result["other"].values()), result["base_bytes"]
        )

    def test_bare_repository_is_recognized(self):
        self.repo(self.root / "bare.git", bare=True)
        result = self.run_audit()
        self.assertEqual(result["summary"]["repo_count"], 1)
        self.assertEqual(
            result["repositories"][0]["bytes"],
            result["repositories"][0]["metadata_bytes"],
        )

    def test_reports_are_reusable_and_escape_unusual_names(self):
        main = self.repo(self.root / "app<&|name")
        self.run_audit()
        result = self.run_audit()
        saved = json.loads((self.output / "disk-audit.json").read_text())
        self.assertEqual(saved["summary"], result["summary"])
        with (self.output / "disk-audit.csv").open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["path"], str(main))
        html = (self.output / "disk-audit.html").read_text()
        self.assertIn("app&lt;&amp;|name", html)
        self.assertIn("<details>", html)
        self.assertIn(r"app<&\|name", (self.output / "disk-audit.md").read_text())

    def test_scan_error_is_recorded_and_remaining_files_are_measured(self):
        self.repo(self.root / "app")
        real_lstat = os.lstat

        def failing_lstat(path, *args, **kwargs):
            if str(path).endswith("/file"):
                raise PermissionError("Fixture access denied")
            return real_lstat(path, *args, **kwargs)

        with patch.object(audit.os, "lstat", side_effect=failing_lstat):
            result = audit.Audit(str(self.root), quiet=True).run()
        self.assertTrue(
            any("Fixture access denied" in e["error"] for e in result["errors"])
        )
        self.assertGreater(result["checkouts"][0]["bytes"], 0)

    def test_directory_with_git_like_names_is_still_measured(self):
        ordinary = self.root / "ordinary"
        ordinary.mkdir()
        for name in ("HEAD", "objects", "refs"):
            (ordinary / name).write_bytes(b"x" * 8192)
        result = self.run_audit()
        self.assertEqual(result["groups"], [])
        self.assertGreaterEqual(result["other"]["ordinary"], 3 * 8192)

    def test_org_only_root_retains_zendesk_grouping(self):
        self.repo(self.root / "zendesk" / "app")
        self.root = self.root / "zendesk"
        result = self.run_audit()
        self.assertEqual(result["repositories"][0]["org"], "Zendesk")

    def test_cli_custom_root_output_and_invalid_root(self):
        self.repo(self.root / "app")
        process = subprocess.run(
            [str(SCRIPT), str(self.root), "--output", str(self.output), "--quiet"],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("1 repositories; 1 checkouts", process.stdout)
        self.assertTrue((self.output / "disk-audit.html").is_file())
        process = subprocess.run(
            [str(SCRIPT), str(self.directory / "missing")],
            capture_output=True,
            check=False,
        )
        self.assertEqual(process.returncode, 2)


if __name__ == "__main__":
    unittest.main()
