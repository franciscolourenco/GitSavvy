"""
Implements a special view to visualize and stage pieces of a project's
current diff.
"""

from collections import namedtuple
from contextlib import contextmanager
from functools import partial
from itertools import chain, dropwhile, takewhile
import os
import re

import sublime
from sublime_plugin import WindowCommand, TextCommand, EventListener

from .navigate import GsNavigate
from ..git_command import GitCommand
from ..exceptions import GitSavvyError
from ...common import util


if False:
    from typing import Callable, Iterable, Iterator, List, NamedTuple, Optional, Set, Tuple, TypeVar
    from mypy_extensions import TypedDict

    T = TypeVar('T')
    ParsedDiff = TypedDict('ParsedDiff', {
        'headers': List[Tuple[int, int]],
        'hunks': List[Tuple[int, int]]
    })

    Point = int
    RowCol = Tuple[int, int]
    HunkLine_ = NamedTuple('HunkLine_', [('mode', str), ('text', str), ('b', int)])


DIFF_TITLE = "DIFF: {}"
DIFF_CACHED_TITLE = "DIFF (cached): {}"

HunkLine = namedtuple('HunkLine', 'mode text b')  # type: HunkLine_
diff_views = {}


class GsDiffCommand(WindowCommand, GitCommand):

    """
    Create a new view to display the difference of `target_commit`
    against `base_commit`. If `target_commit` is None, compare
    working directory with `base_commit`.  If `in_cached_mode` is set,
    display a diff of the Git index. Set `disable_stage` to True to
    disable Ctrl-Enter in the diff view.
    """

    def run(self, **kwargs):
        sublime.set_timeout_async(lambda: self.run_async(**kwargs), 0)

    def run_async(self, in_cached_mode=False, file_path=None, current_file=False, base_commit=None,
                  target_commit=None, disable_stage=False, title=None):
        repo_path = self.repo_path
        if current_file:
            file_path = self.file_path or file_path

        view_key = "{0}{1}+{2}".format(
            in_cached_mode,
            "-" if base_commit is None else "--" + base_commit,
            file_path or repo_path
        )

        if view_key in diff_views and diff_views[view_key] in sublime.active_window().views():
            diff_view = diff_views[view_key]
            self.window.focus_view(diff_view)

        else:
            diff_view = util.view.get_scratch_view(self, "diff", read_only=True)

            settings = diff_view.settings()
            settings.set("git_savvy.repo_path", repo_path)
            settings.set("git_savvy.file_path", file_path)
            settings.set("git_savvy.diff_view.in_cached_mode", in_cached_mode)
            settings.set("git_savvy.diff_view.ignore_whitespace", False)
            settings.set("git_savvy.diff_view.show_word_diff", False)
            settings.set("git_savvy.diff_view.context_lines", 3)
            settings.set("git_savvy.diff_view.base_commit", base_commit)
            settings.set("git_savvy.diff_view.target_commit", target_commit)
            settings.set("git_savvy.diff_view.show_diffstat", self.savvy_settings.get("show_diffstat", True))
            settings.set("git_savvy.diff_view.disable_stage", disable_stage)
            settings.set("git_savvy.diff_view.history", [])
            settings.set("git_savvy.diff_view.just_hunked", "")

            # Clickable lines:
            # (A)  common/commands/view_manipulation.py  |   1 +
            # (B) --- a/common/commands/view_manipulation.py
            # (C) +++ b/common/commands/view_manipulation.py
            # (D) diff --git a/common/commands/view_manipulation.py b/common/commands/view_manipulation.py
            #
            # Now the actual problem is that Sublime only accepts a subset of modern reg expressions,
            # B, C, and D are relatively straight forward because they match a whole line, and
            # basically all other lines in a diff start with one of `[+- ]`.
            FILE_RE = (
                r"^(?:\s(?=.*\s+\|\s+\d+\s)|--- a\/|\+{3} b\/|diff .+b\/)"
                #     ^^^^^^^^^^^^^^^^^^^^^ (A)
                #     ^ one space, and then somewhere later on the line the pattern `  |  23 `
                #                           ^^^^^^^ (B)
                #                                   ^^^^^^^^ (C)
                #                                            ^^^^^^^^^^^ (D)
                r"(\S[^|]*?)"
                #                    ^ ! lazy to not match the trailing spaces, see below

                r"(?:\s+\||$)"
                #          ^ (B), (C), (D)
                #    ^^^^^ (A) We must match the spaces here bc Sublime will not rstrip() the
                #    filename for us.
            )

            settings.set("result_file_regex", FILE_RE)
            # Clickable line:
            # @@ -69,6 +69,7 @@ class GsHandleVintageousCommand(TextCommand):
            #           ^^ we want the second (current) line offset of the diff
            settings.set("result_line_regex", r"^@@ [^+]*\+(\d+)")
            settings.set("result_base_dir", repo_path)

            if not title:
                title = (DIFF_CACHED_TITLE if in_cached_mode else DIFF_TITLE).format(
                    os.path.basename(file_path) if file_path else os.path.basename(repo_path)
                )
            diff_view.set_name(title)
            diff_view.set_syntax_file("Packages/GitSavvy/syntax/diff_view.sublime-syntax")
            diff_views[view_key] = diff_view

            diff_view.run_command("gs_handle_vintageous")


class GsDiffRefreshCommand(TextCommand, GitCommand):
    """Refresh the diff view with the latest repo state."""

    def run(self, edit, sync=True):
        if sync:
            self._run()
        else:
            sublime.set_timeout_async(self._run)

    def _run(self):
        if self.view.settings().get("git_savvy.disable_diff"):
            return
        in_cached_mode = self.view.settings().get("git_savvy.diff_view.in_cached_mode")
        ignore_whitespace = self.view.settings().get("git_savvy.diff_view.ignore_whitespace")
        show_word_diff = self.view.settings().get("git_savvy.diff_view.show_word_diff")
        base_commit = self.view.settings().get("git_savvy.diff_view.base_commit")
        target_commit = self.view.settings().get("git_savvy.diff_view.target_commit")
        show_diffstat = self.view.settings().get("git_savvy.diff_view.show_diffstat")
        disable_stage = self.view.settings().get("git_savvy.diff_view.disable_stage")
        context_lines = self.view.settings().get('git_savvy.diff_view.context_lines')

        prelude = "\n"
        if self.file_path:
            rel_file_path = os.path.relpath(self.file_path, self.repo_path)
            prelude += "  FILE: {}\n".format(rel_file_path)

        if disable_stage:
            if in_cached_mode:
                prelude += "  INDEX..{}\n".format(base_commit or target_commit)
            else:
                if base_commit and target_commit:
                    prelude += "  {}..{}\n".format(base_commit, target_commit)
                else:
                    prelude += "  WORKING DIR..{}\n".format(base_commit or target_commit)
        else:
            if in_cached_mode:
                prelude += "  STAGED CHANGES (Will commit)\n"
            else:
                prelude += "  UNSTAGED CHANGES\n"

        if ignore_whitespace:
            prelude += "  IGNORING WHITESPACE\n"

        try:
            diff = self.git(
                "diff",
                "--ignore-all-space" if ignore_whitespace else None,
                "--word-diff" if show_word_diff else None,
                "--unified={}".format(context_lines) if context_lines is not None else None,
                "--stat" if show_diffstat else None,
                "--patch",
                "--no-color",
                "--cached" if in_cached_mode else None,
                base_commit,
                target_commit,
                "--", self.file_path)
        except GitSavvyError as err:
            # When the output of the above Git command fails to correctly parse,
            # the expected notification will be displayed to the user.  However,
            # once the userpresses OK, a new refresh event will be triggered on
            # the view.
            #
            # This causes an infinite loop of increasingly frustrating error
            # messages, ultimately resulting in psychosis and serious medical
            # bills.  This is a better, though somewhat cludgy, alternative.
            #
            if err.args and type(err.args[0]) == UnicodeDecodeError:
                self.view.settings().set("git_savvy.disable_diff", True)
                return
            raise err

        old_diff = self.view.settings().get("git_savvy.diff_view.raw_diff")
        self.view.settings().set("git_savvy.diff_view.raw_diff", diff)
        text = prelude + '\n--\n' + diff

        self.view.run_command(
            "gs_replace_view_text", {"text": text, "restore_cursors": True}
        )
        if not old_diff:
            self.view.run_command("gs_diff_navigate")


class GsDiffToggleSetting(TextCommand):

    """
    Toggle view settings: `ignore_whitespace` , or `show_word_diff`.
    """

    def run(self, edit, setting):
        settings = self.view.settings()

        setting_str = "git_savvy.diff_view.{}".format(setting)
        current_mode = settings.get(setting_str)
        next_mode = not current_mode
        settings.set(setting_str, next_mode)
        self.view.window().status_message("{} is now {}".format(setting, next_mode))

        self.view.run_command("gs_diff_refresh")


class GsDiffToggleCachedMode(TextCommand):

    """
    Toggle `in_cached_mode` or flip `base` with `target`.
    """

    # NOTE: MUST NOT be async, otherwise `view.show` will not update the view 100%!
    def run(self, edit):
        settings = self.view.settings()

        base_commit = settings.get("git_savvy.diff_view.base_commit")
        target_commit = settings.get("git_savvy.diff_view.target_commit")
        if base_commit and target_commit:
            settings.set("git_savvy.diff_view.base_commit", target_commit)
            settings.set("git_savvy.diff_view.target_commit", base_commit)
            self.view.run_command("gs_diff_refresh")
            return

        last_cursors = settings.get('git_savvy.diff_view.last_cursors') or []
        settings.set('git_savvy.diff_view.last_cursors', pickle_sel(self.view.sel()))

        setting_str = "git_savvy.diff_view.{}".format('in_cached_mode')
        current_mode = settings.get(setting_str)
        next_mode = not current_mode
        settings.set(setting_str, next_mode)
        self.view.window().status_message(
            "Showing {} changes".format("staged" if next_mode else "unstaged")
        )

        self.view.run_command("gs_diff_refresh")

        just_hunked = self.view.settings().get("git_savvy.diff_view.just_hunked")
        # Check for `last_cursors` as well bc it is only falsy on the *first*
        # switch. T.i. if the user hunked and then switches to see what will be
        # actually comitted, the view starts at the top. Later, the view will
        # show the last added hunk.
        if just_hunked and last_cursors:
            self.view.settings().set("git_savvy.diff_view.just_hunked", "")
            region = find_hunk_in_view(self.view, just_hunked)
            if region:
                set_and_show_cursor(self.view, region.a)
                return

        if last_cursors:
            # The 'flipping' between the two states should be as fast as possible and
            # without visual clutter.
            with no_animations():
                set_and_show_cursor(self.view, unpickle_sel(last_cursors))


def find_hunk_in_view(view, patch):
    # type: (sublime.View, str) -> Optional[sublime.Region]
    """Given a patch, search for its first hunk in the view

    Returns the region of the first line of the hunk (the one starting
    with '@@ ...'), if any.
    """
    hunk_content = extract_first_hunk(patch)
    if hunk_content:
        return (
            view.find(hunk_content[0], 0, sublime.LITERAL)
            or fuzzy_search_hunk_content_in_view(view, hunk_content[1:])
        )
    return None


def extract_first_hunk(patch):
    # type: (str) -> Optional[List[str]]
    hunk_lines = patch.split('\n')
    not_hunk_start = lambda line: not line.startswith('@@ ')

    try:
        start, *rest = dropwhile(not_hunk_start, hunk_lines)
    except (StopIteration, ValueError):
        return None

    return [start] + list(takewhile(not_hunk_start, rest))


def fuzzy_search_hunk_content_in_view(view, lines):
    # type: (sublime.View, List[str]) -> Optional[sublime.Region]
    """Fuzzy search the hunk content in the view

    Note that hunk content does not include the starting line, the one
    starting with '@@ ...', anymore.

    The fuzzy strategy here is to search for the hunk or parts of it
    by reducing the contextual lines symmetrically.

    Returns the region of the starting line of the found hunk, if any.
    """
    for hunk_content in shrink_list_sym(lines):
        region = view.find('\n'.join(hunk_content), 0, sublime.LITERAL)
        if region:
            return find_hunk_start_before_pt(view, region.a)
    return None


def shrink_list_sym(list):
    # type: (List[T]) -> Iterator[List[T]]
    while list:
        yield list
        list = list[1:-1]


def find_hunk_start_before_pt(view, pt):
    # type: (sublime.View, int) -> Optional[sublime.Region]
    for region in line_regions_before_pt(view, pt):
        if view.substr(region).startswith('@@ '):
            return region
    return None


def line_regions_before_pt(view, pt):
    # type: (sublime.View, int) -> Iterator[sublime.Region]
    row, _ = view.rowcol(pt)
    for row in reversed(range(row)):
        pt = view.text_point(row, 0)
        yield view.line(pt)


def pickle_sel(sel):
    return [(s.a, s.b) for s in sel]


def unpickle_sel(pickled_sel):
    return [sublime.Region(a, b) for a, b in pickled_sel]


def unique(items):
    # type: (Iterable[T]) -> List[T]
    """Remove duplicate entries but remain sorted/ordered."""
    rv = []  # type: List[T]
    for item in items:
        if item not in rv:
            rv.append(item)
    return rv


def set_and_show_cursor(view, cursors):
    sel = view.sel()
    sel.clear()
    try:
        it = iter(cursors)
    except TypeError:
        sel.add(cursors)
    else:
        for c in it:
            sel.add(c)

    view.show(sel)


@contextmanager
def no_animations():
    pref = sublime.load_settings("Preferences.sublime-settings")
    current = pref.get("animation_enabled")
    pref.set("animation_enabled", False)
    try:
        yield
    finally:
        pref.set("animation_enabled", current)


def parse_diff_in_view(view):
    # type: (sublime.View) -> ParsedDiff
    header_starts = tuple(region.a for region in view.find_all("^diff"))
    header_ends = tuple(region.b for region in view.find_all(r"^\+\+\+.+\n(?=@@)"))
    hunk_starts = tuple(region.a for region in view.find_all("^@@"))
    hunk_ends = tuple(sorted(list(
        # Hunks end when the next diff starts.
        set(header_starts[1:]) |
        # Hunks end when the next hunk starts, except for hunks
        # immediately following diff headers.
        (set(hunk_starts) - set(header_ends)) |
        # The last hunk ends at the end of the file.
        # It should include the last line (`+ 1`).
        set((view.size() + 1, ))
    )))

    return {
        'headers': list(zip(header_starts, header_ends)),
        'hunks': list(zip(hunk_starts, hunk_ends))
    }


def head_and_hunk_for_pt(diff, pt):
    # type: (ParsedDiff, int) -> Optional[Tuple[Tuple[int, int], Tuple[int, int]]]
    """Return header and hunk offsets for given point if any"""
    for hunk_start, hunk_end in diff['hunks']:
        if hunk_start <= pt < hunk_end:
            break
    else:
        return None

    header_start, header_end = max(
        (header_start, header_end)
        for header_start, header_end in diff['headers']
        if (header_start, header_end) < (hunk_start, hunk_end)
    )

    header = header_start, header_end
    hunk = hunk_start, hunk_end

    return header, hunk


def extract_content(view, region):
    # type: (sublime.View, Tuple[int, int]) -> str
    return view.substr(sublime.Region(*region))


filter_ = partial(filter, None)  # type: Callable[[Iterator[Optional[T]]], Iterator[T]]


class GsDiffZoom(TextCommand):
    """
    Update the number of context lines the diff shows by given `amount`
    and refresh the view.
    """
    def run(self, edit, amount):
        # type: (sublime.Edit, int) -> None
        settings = self.view.settings()
        current = settings.get('git_savvy.diff_view.context_lines')
        next = max(current + amount, 0)
        settings.set('git_savvy.diff_view.context_lines', next)

        # Getting a meaningful cursor after 'zooming' is the tricky part
        # here. We first extract all hunks under the cursors *verbatim*.
        diff = parse_diff_in_view(self.view)
        extract = partial(extract_content, self.view)
        cur_hunks = [
            extract(header) + extract(hunk)
            for header, hunk in filter_(head_and_hunk_for_pt(diff, s.a) for s in self.view.sel())
        ]

        self.view.run_command("gs_diff_refresh")

        # Now, we fuzzy search the new view content for the old hunks.
        cursors = {
            region.a
            for region in (
                filter_(find_hunk_in_view(self.view, hunk) for hunk in cur_hunks)
            )
        }
        if cursors:
            set_and_show_cursor(self.view, cursors)


class GsDiffFocusEventListener(EventListener):

    """
    If the current view is a diff view, refresh the view with latest tree status
    when the view regains focus.
    """

    def on_activated_async(self, view):
        if view.settings().get("git_savvy.diff_view") is True:
            view.run_command("gs_diff_refresh", {"sync": False})


class GsDiffStageOrResetHunkCommand(TextCommand, GitCommand):

    """
    Depending on whether the user is in cached mode and what action
    the user took, either 1) stage, 2) unstage, or 3) reset the
    hunk under the user's cursor(s).
    """

    # NOTE: The whole command (including the view refresh) must be blocking otherwise
    # the view and the repo state get out of sync and e.g. hitting 'h' very fast will
    # result in errors.

    def run(self, edit, reset=False):
        ignore_whitespace = self.view.settings().get("git_savvy.diff_view.ignore_whitespace")
        show_word_diff = self.view.settings().get("git_savvy.diff_view.show_word_diff")
        if ignore_whitespace or show_word_diff:
            sublime.error_message("You have to be in a clean diff to stage.")
            return None

        # Filter out any cursors that are larger than a single point.
        cursor_pts = tuple(cursor.a for cursor in self.view.sel() if cursor.a == cursor.b)
        diff = parse_diff_in_view(self.view)

        extract = partial(extract_content, self.view)
        flatten = chain.from_iterable

        patches = unique(flatten(filter_(head_and_hunk_for_pt(diff, pt) for pt in cursor_pts)))
        patch = ''.join(map(extract, patches))

        if patch:
            self.apply_patch(patch, cursor_pts, reset)
        else:
            window = self.view.window()
            if window:
                window.status_message('Not within a hunk')

    def apply_patch(self, patch, pts, reset):
        in_cached_mode = self.view.settings().get("git_savvy.diff_view.in_cached_mode")
        context_lines = self.view.settings().get('git_savvy.diff_view.context_lines')

        # The three argument combinations below result from the following
        # three scenarios:
        #
        # 1) The user is in non-cached mode and wants to stage a hunk, so
        #    do NOT apply the patch in reverse, but do apply it only against
        #    the cached/indexed file (not the working tree).
        # 2) The user is in non-cached mode and wants to undo a line/hunk, so
        #    DO apply the patch in reverse, and do apply it both against the
        #    index and the working tree.
        # 3) The user is in cached mode and wants to undo a line hunk, so DO
        #    apply the patch in reverse, but only apply it against the cached/
        #    indexed file.
        #
        # NOTE: When in cached mode, no action will be taken when the user
        #       presses SUPER-BACKSPACE.

        args = (
            "apply",
            "-R" if (reset or in_cached_mode) else None,
            "--cached" if (in_cached_mode or not reset) else None,
            "--unidiff-zero" if context_lines == 0 else None,
            "-",
        )
        self.git(
            *args,
            stdin=patch
        )

        history = self.view.settings().get("git_savvy.diff_view.history")
        history.append((args, patch, pts, in_cached_mode))
        self.view.settings().set("git_savvy.diff_view.history", history)
        self.view.settings().set("git_savvy.diff_view.just_hunked", patch)

        self.view.run_command("gs_diff_refresh")


class GsDiffOpenFileAtHunkCommand(TextCommand, GitCommand):

    """
    For each cursor in the view, identify the hunk in which the cursor lies,
    and open the file at that hunk in a separate view.
    """

    def run(self, edit):
        # type: (sublime.Edit) -> None
        # Filter out any cursors that are larger than a single point.
        cursor_pts = tuple(cursor.a for cursor in self.view.sel() if cursor.a == cursor.b)

        def first_per_file(items):
            # type: (Iterator[Tuple[str, int, int]]) -> Iterator[Tuple[str, int, int]]
            seen = set()  # type: Set[str]
            for item in items:
                filename, _, _ = item
                if filename not in seen:
                    seen.add(filename)
                    yield item

        diff = parse_diff_in_view(self.view)
        jump_positions = filter_(self.jump_position_to_file(diff, pt) for pt in cursor_pts)
        for jp in first_per_file(jump_positions):
            self.load_file_at_line(*jp)

    def load_file_at_line(self, filename, row, col):
        # type: (str, int, int) -> None
        """
        Show file at target commit if `git_savvy.diff_view.target_commit` is non-empty.
        Otherwise, open the file directly.
        """
        target_commit = self.view.settings().get("git_savvy.diff_view.target_commit")
        full_path = os.path.join(self.repo_path, filename)
        window = self.view.window()
        if not window:
            return

        if target_commit:
            window.run_command("gs_show_file_at_commit", {
                "commit_hash": target_commit,
                "filepath": full_path,
                "lineno": row,
            })
        else:
            window.open_file(
                "{file}:{row}:{col}".format(file=full_path, row=row, col=col),
                sublime.ENCODED_POSITION
            )

    def jump_position_to_file(self, diff, pt):
        # type: (ParsedDiff, int) -> Optional[Tuple[str, int, int]]
        head_and_hunk_offsets = head_and_hunk_for_pt(diff, pt)
        if not head_and_hunk_offsets:
            return None

        view = self.view
        header_region, hunk_region = head_and_hunk_offsets
        header = extract_content(view, header_region)
        hunk = extract_content(view, hunk_region)
        hunk_start, _ = hunk_region

        rowcol = real_rowcol_in_hunk(hunk, relative_rowcol_in_hunk(view, hunk_start, pt))
        if not rowcol:
            return None

        row, col = rowcol

        filename = extract_filename_from_header(header)
        if not filename:
            return None

        return filename, row, col


def relative_rowcol_in_hunk(view, hunk_start, pt):
    # type: (sublime.View, Point, Point) -> RowCol
    """Return rowcol of given pt relative to hunk start"""
    head_row, _ = view.rowcol(hunk_start)
    pt_row, col = view.rowcol(pt)
    # If `col=0` the user is on the meta char (e.g. '+- ') which is not
    # present in the source. We pin `col` to 1 because the target API
    # `open_file` expects 1-based row, col offsets.
    return pt_row - head_row, max(col, 1)


def real_rowcol_in_hunk(hunk, relative_rowcol):
    # type: (str, RowCol) -> Optional[RowCol]
    """Translate relative to absolute row, col pair"""
    hunk_lines = split_hunk(hunk)
    if not hunk_lines:
        return None

    row_in_hunk, col = relative_rowcol

    # If the user is on the header line ('@@ ..') pretend to be on the
    # first visible line with some content instead.
    if row_in_hunk == 0:
        row_in_hunk = next(
            (
                index
                for index, line in enumerate(hunk_lines, 1)
                if line.mode in ('+', ' ') and line.text.strip()
            ),
            1
        )
        col = 1

    line = hunk_lines[row_in_hunk - 1]

    # Happy path since the user is on a present line
    if line.mode != '-':
        return line.b, col

    # The user is on a deleted line ('-') we cannot jump to. If possible,
    # select the next guaranteed to be available line
    for next_line in hunk_lines[row_in_hunk:]:
        if next_line.mode == '+':
            return next_line.b, min(col, len(next_line.text) + 1)
        elif next_line.mode == ' ':
            # If we only have a contextual line, choose this or the
            # previous line, pretty arbitrary, depending on the
            # indentation.
            next_lines_indentation = line_indentation(next_line.text)
            if next_lines_indentation == line_indentation(line.text):
                return next_line.b, next_lines_indentation + 1
            else:
                return max(1, line.b - 1), 1
    else:
        return line.b, 1


HUNKS_LINES_RE = re.compile(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? ')


def split_hunk(hunk):
    # type: (str) -> Optional[List[HunkLine]]
    """Split a hunk into (first char, line content, row) tuples

    Note that rows point to available rows on the b-side.
    """

    head, *tail = hunk.rstrip().split('\n')
    match = HUNKS_LINES_RE.search(head)
    if not match:
        return None

    b = int(match.group(2))
    return list(_recount_lines(tail, b))


def _recount_lines(lines, b):
    # type: (List[str], int) -> Iterator[HunkLine]

    # Be aware that we only consider the b-line numbers, and that we
    # always yield a b value, even for deleted lines.
    for line in lines:
        first_char, tail = line[0], line[1:]
        yield HunkLine(first_char, tail, b)

        if first_char != '-':
            b += 1


def line_indentation(line):
    # type: (str) -> int
    return len(line) - len(line.lstrip())


HEADER_TO_FILE_RE = re.compile(r'\+\+\+ b/(.+)$')


def extract_filename_from_header(header):
    # type: (str) -> Optional[str]
    match = HEADER_TO_FILE_RE.search(header)
    if not match:
        return None

    return match.group(1)


class GsDiffNavigateCommand(GsNavigate):

    """
    Travel between hunks. It is also used by show_commit_view.
    """

    offset = 0

    def get_available_regions(self):
        return [self.view.line(region) for region in
                self.view.find_by_selector("meta.diff.range.unified")]


class GsDiffUndo(TextCommand, GitCommand):

    """
    Undo the last action taken in the diff view, if possible.
    """

    # NOTE: MUST NOT be async, otherwise `view.show` will not update the view 100%!
    def run(self, edit):
        history = self.view.settings().get("git_savvy.diff_view.history")
        if not history:
            window = self.view.window()
            if window:
                window.status_message("Undo stack is empty")
            return

        args, stdin, cursors, in_cached_mode = history.pop()
        # Toggle the `--reverse` flag.
        args[1] = "-R" if not args[1] else None

        self.git(*args, stdin=stdin)
        self.view.settings().set("git_savvy.diff_view.history", history)
        self.view.settings().set("git_savvy.diff_view.just_hunked", stdin)

        self.view.run_command("gs_diff_refresh")

        # The cursor is only applicable if we're still in the same cache/stage mode
        if self.view.settings().get("git_savvy.diff_view.in_cached_mode") == in_cached_mode:
            set_and_show_cursor(self.view, cursors)
