#!/usr/bin/env python3
"""Choose cleanup actions before Git removes any worktree or branch."""
import curses
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import textwrap


@dataclass
class Entry:
    path: str
    branch: str
    tip: str
    mode: str
    reason: str

    @property
    def eligible(self):
        return self.mode in ('retain', 'delete')

    @property
    def label(self):
        return self.branch or '(detached HEAD)'

    @property
    def action(self):
        if not self.eligible:
            return 'Keep: ' + self.reason
        if not self.path:
            return 'Delete merged branch ' + self.branch
        branch_action = 'retain' if self.mode == 'retain' else 'delete merged'
        return f'Remove worktree "{self.path}" and {branch_action} branch "{self.branch}"'


def display(value):
    """Do not let path names inject terminal controls or extra rows."""
    return ''.join(c if c.isprintable() else ascii(c)[1:-1] for c in value)


def read_entries(path):
    fields = Path(path).read_bytes().split(b'\0')
    if fields[-1] != b'' or (len(fields) - 1) % 5:
        raise ValueError('Invalid cleanup inventory')
    return [Entry(*(os.fsdecode(f) for f in fields[i:i + 5]))
            for i in range(0, len(fields) - 1, 5)]


class Checklist:
    def __init__(self, entries):
        self.entries = entries
        self.cursor = 0
        self.selected = set()

    def move(self, amount):
        self.cursor = max(0, min(len(self.entries) - 1, self.cursor + amount))

    def toggle(self):
        if not self.entries[self.cursor].eligible:
            return False
        if self.cursor in self.selected:
            self.selected.remove(self.cursor)
        else:
            self.selected.add(self.cursor)
        return True

    def toggle_all(self):
        eligible = {i for i, e in enumerate(self.entries) if e.eligible}
        self.selected = set() if self.selected == eligible else eligible

    def summary(self):
        entries = [self.entries[i] for i in self.selected]
        worktrees = sum(bool(e.path) for e in entries)
        delete = sum(e.mode == 'delete' for e in entries)
        retain = sum(e.mode == 'retain' for e in entries)
        return (f'Remove {worktrees} worktree{"" if worktrees == 1 else "s"}; '
                f'delete {delete} branch{"" if delete == 1 else "es"}; '
                f'retain {retain} branch{"" if retain == 1 else "es"}.')


def parse_selection(text, entries):
    eligible = [i for i, e in enumerate(entries) if e.eligible]
    if text.strip().lower() == 'all':
        return set(eligible)
    if text.strip().lower() in ('', 'none', 'q', 'quit'):
        return set()
    selected = set()
    for token in text.replace(',', ' ').split():
        bounds = token.split('-')
        if len(bounds) == 1:
            first = last = int(bounds[0])
        elif len(bounds) == 2:
            first, last = map(int, bounds)
        else:
            raise ValueError('Use numbers, ranges, or all')
        if not 1 <= first <= last <= len(eligible):
            raise ValueError('Selection number is out of range')
        selected.update(eligible[i - 1] for i in range(first, last + 1))
    return selected


def text_checklist(entries):
    checklist = Checklist(entries)
    print('\nCleanup checklist — nothing is removed until batch confirmation.')
    ordinal = 0
    for entry in entries:
        if entry.eligible:
            ordinal += 1
            print(f'{ordinal:3} [ ] {display(entry.label)}')
            print('        ' + display(entry.action))
            print('        ' + display(entry.reason))
        else:
            print(f'    [-] {display(entry.label)} — {display(entry.reason)}')
        if entry.path and not entry.eligible:
            print('        ' + display(entry.path))
    if not ordinal:
        print('No eligible cleanup actions.')
        return None
    while True:
        try:
            answer = input('\nSelect numbers/ranges or all (blank cancels): ')
        except EOFError:
            return None
        try:
            checklist.selected = parse_selection(answer, entries)
        except ValueError as error:
            print(f'Invalid selection: {error}.')
            continue
        if not checklist.selected:
            return None
        print(checklist.summary())
        print('Ignored files inside selected worktrees will also be removed.')
        try:
            answer = input('Apply this batch? [y/N] ')
        except EOFError:
            return None
        if answer.lower() in ('y', 'yes'):
            return sorted(checklist.selected)
        print('Batch not applied. Change the selection or leave it blank to cancel.')


def terminal_checklist(screen, entries):
    checklist = Checklist(entries)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.keypad(True)
    top = 0
    review = False
    message = ''

    def put(row, value, attr=0):
        height, width = screen.getmaxyx()
        if 0 <= row < height and width > 1:
            try:
                screen.addnstr(row, 0, display(value), width - 1, attr)
            except curses.error:
                pass

    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 12 or width < 40:
            put(0, 'Resize terminal to at least 40 columns x 12 rows.')
            put(1, 'q / Esc: cancel')
        elif review:
            put(0, f'Review batch — {len(checklist.selected)} selected actions', curses.A_BOLD)
            row = 2
            for paragraph in (checklist.summary(),
                              'Ignored files inside selected worktrees will also be removed.',
                              'y: apply batch   n / Esc: back   q: cancel'):
                for line in textwrap.wrap(paragraph, width - 1):
                    put(row, line)
                    row += 1
                row += 1
            available = max(0, height - row - 1)
            for index in sorted(checklist.selected)[:available]:
                entry = entries[index]
                action = 'keep branch' if entry.mode == 'retain' else 'delete branch'
                put(row, f'  {entry.label} — {action}')
                row += 1
            if len(checklist.selected) > available:
                put(height - 1, f'  ... {len(checklist.selected) - available} more selected (all included).')
        else:
            eligible = sum(e.eligible for e in entries)
            put(0, f'Git cleanup — {len(checklist.selected)} selected / {eligible} eligible / {len(entries)} entries', curses.A_BOLD)
            if width >= 75:
                put(1, '↑/↓ j/k: move  Space: toggle  a: all/none  Enter: review  q/Esc: cancel')
            else:
                put(1, '↑↓/jk move  Space toggle  a all/none')
                put(2, 'Enter review  q/Esc cancel')
            visible = height - 9
            if checklist.cursor < top:
                top = checklist.cursor
            if checklist.cursor >= top + visible:
                top = checklist.cursor - visible + 1
            for row, index in enumerate(range(top, min(len(entries), top + visible)), 3):
                entry = entries[index]
                marker = 'x' if index in checklist.selected else (' ' if entry.eligible else '-')
                action = ('retain branch' if entry.mode == 'retain' else 'delete branch') if entry.eligible else 'keep'
                # Action comes first so long branch names cannot hide it.
                label = f'[{marker}] {action:13} {entry.label}'
                attr = curses.A_REVERSE if index == checklist.cursor else 0
                if index in checklist.selected:
                    attr |= curses.A_BOLD
                put(row, label, attr)
            entry = entries[checklist.cursor]
            detail = f'{entry.label} | {entry.path or "No worktree"} | {entry.reason}'
            for row, line in enumerate(textwrap.wrap(display(detail), max(1, width - 1))[:3], height - 5):
                put(row, line)
            put(height - 2, f'Entry {checklist.cursor + 1}/{len(entries)} · {message}')
            put(height - 1, 'Enter reviews the batch. No removal happens while you select.')
        screen.refresh()
        key = screen.getch()
        if key in (ord('q'), 3):
            return None
        if height < 12 or width < 40:
            if key == 27:
                return None
            continue
        if review:
            if key in (ord('y'), ord('Y')):
                return sorted(checklist.selected)
            if key in (27, ord('n'), ord('N'), curses.KEY_BACKSPACE):
                review = False
            continue
        if key == 27:
            return None
        if key in (curses.KEY_DOWN, ord('j')):
            checklist.move(1)
        elif key in (curses.KEY_UP, ord('k')):
            checklist.move(-1)
        elif key == curses.KEY_NPAGE:
            checklist.move(max(1, height - 9))
        elif key == curses.KEY_PPAGE:
            checklist.move(-max(1, height - 9))
        elif key == curses.KEY_HOME:
            checklist.cursor = 0
        elif key == curses.KEY_END:
            checklist.cursor = len(entries) - 1
        elif key == ord(' '):
            message = '' if checklist.toggle() else 'Kept: ' + entries[checklist.cursor].reason
        elif key == ord('a'):
            checklist.toggle_all()
            message = ''
        elif key in (10, 13, curses.KEY_ENTER):
            if checklist.selected:
                review = True
            else:
                message = 'Select at least one entry with Space or a.'


def main():
    if len(sys.argv) != 3:
        print('Usage: git-cleanup-select.py INVENTORY OUTPUT', file=sys.stderr)
        return 2
    inventory, output = map(Path, sys.argv[1:])
    # Cancellation must not leave a stale selection that a caller could apply.
    output.unlink(missing_ok=True)
    try:
        entries = read_entries(inventory)
        if not entries:
            print('No cleanup entries.')
            return 130
        if sys.stdin.isatty() and sys.stdout.isatty():
            selected = curses.wrapper(terminal_checklist, entries)
        else:
            selected = text_checklist(entries)
        if not selected:
            return 130
        output.write_text(''.join(f'{i}\n' for i in selected))
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, curses.error) as error:
        print(f'Could not display cleanup checklist: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
