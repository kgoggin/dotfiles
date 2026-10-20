import fcntl
import importlib.util
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest


HELPER = Path(__file__).resolve().parents[1] / 'git-cleanup-select.py'
spec = importlib.util.spec_from_file_location('cleanup_select', HELPER)
ui = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ui
spec.loader.exec_module(ui)


class ChecklistTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cleanup-selector-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entries = [
            ui.Entry('/repo', 'main', 'a', 'keep', 'protected checkout'),
            ui.Entry('/first', 'first', 'b', 'retain', 'idle: last commit 8 days ago'),
            ui.Entry('/second', 'second', 'c', 'delete', 'merged into origin/main'),
        ]
        self.inventory = self.root / 'inventory'
        self.output = self.root / 'selection'
        self.write_inventory()

    def write_inventory(self):
        self.inventory.write_bytes(b''.join(
            os.fsencode(field) + b'\0'
            for e in self.entries
            for field in (e.path, e.branch, e.tip, e.mode, e.reason)))

    def run_text(self, answer):
        return subprocess.run([sys.executable, str(HELPER), str(self.inventory), str(self.output)],
                              input=answer, text=True, capture_output=True)

    def test_toggle_navigation_and_disabled_rows(self):
        model = ui.Checklist(self.entries)
        self.assertFalse(model.toggle())
        model.move(1)
        self.assertTrue(model.toggle())
        model.move(20)
        self.assertEqual(model.cursor, 2)
        model.toggle()
        model.move(-1)
        model.toggle()
        self.assertEqual(model.selected, {2})
        model.move(-20)
        self.assertEqual(model.cursor, 0)

    def test_toggle_all_excludes_protected_rows_and_can_clear(self):
        model = ui.Checklist(self.entries)
        model.toggle_all()
        self.assertEqual(model.selected, {1, 2})
        model.toggle_all()
        self.assertEqual(model.selected, set())

    def test_summary_distinguishes_retained_and_deleted_branches(self):
        self.entries.append(ui.Entry('', 'orphan', 'd', 'delete', 'merged branch'))
        model = ui.Checklist(self.entries)
        model.toggle_all()
        self.assertEqual(model.summary(),
                         'Remove 2 worktrees; delete 2 branches; retain 1 branch.')

    def test_number_ranges_count_only_eligible_rows(self):
        self.assertEqual(ui.parse_selection('1-2, 2', self.entries), {1, 2})
        self.assertEqual(ui.parse_selection('2', self.entries), {2})
        self.assertEqual(ui.parse_selection('all', self.entries), {1, 2})
        for value in ('0', '3', '2-1', '1-500000000', 'bad', '1-2-3'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ui.parse_selection(value, self.entries)

    def test_nul_inventory_preserves_unusual_paths(self):
        self.entries[1].path = '/a path/with "quotes" and\nnewlines'
        self.write_inventory()
        self.assertEqual(ui.read_entries(self.inventory), self.entries)
        self.assertNotIn('\n', ui.display(self.entries[1].path))
        self.assertNotIn('\x1b', ui.display('escape\x1b[31m'))

    def test_invalid_inventory_is_rejected(self):
        self.inventory.write_bytes(b'not\0a\0complete\0record')
        result = self.run_text('all\ny\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_text_selection_needs_final_confirmation(self):
        result = self.run_text('all\n')
        self.assertEqual(result.returncode, 130)
        self.assertFalse(self.output.exists())
        self.assertIn('Apply this batch?', result.stdout)

    def test_text_selection_writes_only_the_chosen_inventory_id(self):
        result = self.run_text('2\ny\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), '2\n')
        self.assertIn('main', result.stdout)
        self.assertIn('protected checkout', result.stdout)
        self.assertIn('retain branch "first"', result.stdout)
        self.assertIn('delete merged branch "second"', result.stdout)

    def test_cancel_removes_stale_output_and_does_not_apply(self):
        self.output.write_text('1\n')
        result = self.run_text('\n')
        self.assertEqual(result.returncode, 130)
        self.assertFalse(self.output.exists())

    def test_declining_review_allows_selection_to_change(self):
        result = self.run_text('all\nn\n1\ny\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), '1\n')

    def run_terminal(self, keys):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
        before = termios.tcgetattr(slave)
        process = subprocess.Popen(
            [sys.executable, str(HELPER), str(self.inventory), str(self.output)],
            stdin=slave, stdout=slave, stderr=slave,
            env=dict(os.environ, TERM='xterm-256color'))
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        data = b''
        deadline = time.monotonic() + 8
        while b'Enter reviews the batch.' not in data:
            if time.monotonic() > deadline:
                self.fail(f'Checklist did not start: {data!r}')
            if select.select([master], [], [], 0.1)[0]:
                data += os.read(master, 65536)
            if process.poll() is not None:
                self.fail(f'Checklist exited early: {data!r}')
        os.write(master, keys)
        deadline = time.monotonic() + 8
        while process.poll() is None:
            if time.monotonic() > deadline:
                self.fail(f'Checklist did not accept keys {keys!r}: {data!r}')
            if select.select([master], [], [], 0.1)[0]:
                data += os.read(master, 65536)
        process.wait(timeout=1)
        after = termios.tcgetattr(slave)
        # macOS may set PENDIN after queued input is processed; it is kernel
        # input state, not an altered echo/canonical terminal mode.
        for attrs in (before, after):
            attrs[3] &= ~getattr(termios, 'PENDIN', 0)
        self.assertEqual(after, before,
                         'Checklist must restore the terminal after exiting')
        return process.returncode

    def test_terminal_navigation_toggles_then_confirms_one_batch(self):
        # xterm application cursor-down, Space, then j/Space, k/Space:
        # select first and second, then deselect first. Only second remains.
        result = self.run_terminal(b'\x1bOB j k \ny')
        self.assertEqual(result, 0)
        self.assertEqual(self.output.read_text(), '2\n')

    def test_terminal_review_can_return_to_checklist_before_cancel(self):
        result = self.run_terminal(b'ja\nnq')
        self.assertEqual(result, 130)
        self.assertFalse(self.output.exists())

    def test_terminal_cancel_keeps_everything_unselected(self):
        result = self.run_terminal(b'jaq')
        self.assertEqual(result, 130)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
