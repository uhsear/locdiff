#!/usr/bin/env python
"""Diff two ArcGIS locator .loc files and fail when a matching setting was lost.

A rebuilt locator writes a fresh .loc. Settings that were set by hand on the old
build do not come with it. The one that hurts is supportsOptionalZone ("Match
with no zones" in ArcGIS Pro). Without it, an address typed with no city can
stop matching on the server while every other search still works.

This reads both .loc files as key = value text, folds key case, ignores the two
rebuild time stamps, and lists what the new build dropped, added or changed.
It exits 1 when a watched key (supportsOptionalZone by default) was dropped or
changed. With --apply it copies the old value of each such key into the new
.loc, after it writes a numbered backup. Nothing else writes.

    python locdiff.py --self-test
    python locdiff.py old/Streets.loc new/Streets.loc
    python locdiff.py old/Streets.loc new/Streets.loc --watch ComprehensiveZoneMatch
    python locdiff.py old/Streets.loc new/Streets.loc --apply

Exit codes: 0 no watched key lost, 1 a watched key was dropped, changed or is
ambiguous, 2 a file could not be read or written, 64 usage error.
"""

from __future__ import print_function

import argparse
import contextlib
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile

DEFAULT_WATCH = "supportsOptionalZone"

# Defaults Esri documents for a key that is absent. BUG-000127758: "If the
# property is set to false, which is the default value, the locator requires a
# zone when searching." Dropping a key that held its default changes nothing.
DOCUMENTED_DEFAULTS = {"supportsoptionalzone": "false"}

# Rewritten by every rebuild, so a diff that shows them is noise. Both appear in
# Esri's own sample .loc files. Add more with --ignore.
BUILD_STAMP_KEYS = ("RebuildStartTime", "RebuildStartTimeAscii")

KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.]*$")

# A UTF-8 BOM, as text. Written as chr() so the source stays pure ASCII.
BOM = chr(0xFEFF)

# Verdicts for one watched key.
SAME = "SAME"
ABSENT = "ABSENT"
ADDED = "ADDED"
DROPPED = "DROPPED"
DROPPED_DEFAULT = "DROPPED-DEFAULT"
CHANGED = "CHANGED"
AMBIGUOUS = "AMBIGUOUS"
FAILING = (DROPPED, CHANGED, AMBIGUOUS)
RESTORABLE = (DROPPED, CHANGED)


# ----------------------------------------------------------------- pure core

class Entry(object):
    """One live key = value line of a .loc file."""

    def __init__(self, line, key, value):
        self.line = line
        self.key = key
        self.value = value

    def __repr__(self):
        return "Entry(%d, %r, %r)" % (self.line, self.key, self.value)


def parse_line(line):
    """(key, value) for a live setting line, or None for anything else.

    A line that starts with ';' is a comment, and Pro writes disabled settings
    as '; KEY =': KEY_RE refuses a key that starts with ';', so a commented
    key counts as absent. Only the first '=' splits: LocatorWorkspaceString
    holds 'ProgID = ...; Path = ...'.
    """
    body = line.lstrip(BOM).strip()
    if "=" not in body:
        return None
    key, value = body.split("=", 1)
    key = key.strip()
    if not KEY_RE.match(key):
        return None
    return key, value.strip()


def parse_loc(text):
    """Every live setting in a .loc text, in file order, with 1-based lines."""
    entries = []
    for number, line in enumerate(text.split("\n"), 1):
        parsed = parse_line(line)
        if parsed is not None:
            entries.append(Entry(number, parsed[0], parsed[1]))
    return entries


def norm(value):
    """Fold a boolean's case. Esri files write True, true and TRUE alike."""
    low = value.lower()
    return low if low in ("true", "false") else value


def index(entries):
    """Folded key -> the entries that set it, in file order.

    A key may repeat on purpose (BatchPresortInputs lists one field per line),
    so a key maps to a list, never to one value that the last line overwrote.
    """
    out = {}
    for entry in entries:
        out.setdefault(entry.key.lower(), []).append(entry)
    return out


def distinct(entries):
    """The distinct normalised values of a key, in first-seen order."""
    seen = []
    for entry in entries:
        value = norm(entry.value)
        if value not in seen:
            seen.append(value)
    return seen


class Finding(object):
    """The verdict on one watched key."""

    def __init__(self, key, verdict, old, new, message):
        self.key = key
        self.verdict = verdict
        self.old = old
        self.new = new
        self.message = message


def _lines_of(entries):
    return ", ".join(str(e.line) for e in entries)


def judge(key, old, new):
    """Decide one watched key from its OLD and NEW entries."""
    folded = key.lower()
    old_values, new_values = distinct(old), distinct(new)
    for side, entries, values in (("OLD", old, old_values),
                                  ("NEW", new, new_values)):
        if len(values) > 1:
            return Finding(key, AMBIGUOUS, old, new,
                           "%s sets it on lines %s to %s. Which line the "
                           "reader uses is not documented."
                           % (side, _lines_of(entries), " and ".join(values)))
    if not old and not new:
        return Finding(key, ABSENT, old, new, "absent from both files")
    if not new:
        if DOCUMENTED_DEFAULTS.get(folded) == old_values[0]:
            return Finding(key, DROPPED_DEFAULT, old, new,
                           "OLD line %d = %s, absent from NEW. %s is the "
                           "documented default, so nothing changes."
                           % (old[0].line, old[0].value, old[0].value))
        return Finding(key, DROPPED, old, new,
                       "OLD line %d = %s, absent from NEW"
                       % (old[0].line, old[0].value))
    if not old:
        return Finding(key, ADDED, old, new,
                       "absent from OLD, NEW line %d = %s"
                       % (new[0].line, new[0].value))
    if old_values != new_values:
        return Finding(key, CHANGED, old, new,
                       "OLD line %d = %s, NEW line %d = %s"
                       % (old[0].line, old[0].value, new[0].line,
                          new[0].value))
    return Finding(key, SAME, old, new, "%s on both sides" % new[0].value)


class Report(object):
    """Everything the diff found, with no file involved."""

    def __init__(self):
        self.watched = []
        self.only_old = []
        self.only_new = []
        self.differs = []
        self.respelled = []
        self.ignored = []

    @property
    def failed(self):
        return any(f.verdict in FAILING for f in self.watched)


def compare(old_entries, new_entries, watch=(DEFAULT_WATCH,), ignore=()):
    """Diff two parsed .loc files. Key case is folded throughout.

    Esri's own files spell the same key two ways (supportsOptionalZone in one
    sample, SupportsOptionalZone in a Pro build), so a case-sensitive diff
    would report one renamed key as one drop plus one add.
    """
    old, new = index(old_entries), index(new_entries)
    report = Report()
    watched = []
    for key in watch:
        if key.lower() not in [w.lower() for w in watched]:
            watched.append(key)
    folded_watch = set(w.lower() for w in watched)
    skip = set(k.lower() for k in BUILD_STAMP_KEYS) | set(k.lower()
                                                          for k in ignore)
    for key in watched:
        report.watched.append(judge(key, old.get(key.lower(), []),
                                    new.get(key.lower(), [])))
    for folded in sorted(set(old) | set(new)):
        o, n = old.get(folded, []), new.get(folded, [])
        if o and n:
            o_names = sorted(set(e.key for e in o))
            n_names = sorted(set(e.key for e in n))
            if o_names != n_names:
                report.respelled.append((", ".join(o_names),
                                         ", ".join(n_names)))
        if folded in folded_watch:
            continue
        if folded in skip:
            report.ignored.append((o or n)[0].key)
            continue
        if not n:
            report.only_old.append(o)
        elif not o:
            report.only_new.append(n)
        elif [norm(e.value) for e in o] != [norm(e.value) for e in n]:
            report.differs.append((o, n))
    return report


def plan_edits(report):
    """(key, value) pairs to copy from OLD into NEW. Refuses on AMBIGUOUS.

    The key keeps OLD's spelling only where NEW has no line for it: a line NEW
    already has is rewritten in place under NEW's own spelling.
    """
    vague = [f.key for f in report.watched if f.verdict == AMBIGUOUS]
    if vague:
        raise ValueError("%s is set twice with different values, so there is "
                         "no one value to restore. Nothing was written."
                         % ", ".join(vague))
    return [(f.old[0].key, f.old[0].value) for f in report.watched
            if f.verdict in RESTORABLE]


def apply_edits(text, edits):
    """The .loc text with each (key, value) set. Every other byte is kept.

    Every live line for the key is rewritten, so a key set twice ends with one
    value. A key with no live line is appended, in the file's own line ending.
    """
    lines = text.split("\n")
    crlf = "\r\n" in text
    appended = []
    for key, value in edits:
        hit = False
        for i, line in enumerate(lines):
            parsed = parse_line(line)
            if parsed is not None and parsed[0].lower() == key.lower():
                lead = BOM if line.startswith(BOM) else ""
                cr = "\r" if line.endswith("\r") else ""
                lines[i] = "%s%s = %s%s" % (lead, parsed[0], value, cr)
                hit = True
        if not hit:
            appended.append("%s = %s%s" % (key, value, "\r" if crlf else ""))
    if not appended:
        return "\n".join(lines)
    if lines[-1] == "":
        lines.pop()
    elif crlf:
        lines[-1] += "\r"
    return "\n".join(lines + appended + [""])


def show(value):
    """File text made safe for any console: non-ASCII becomes an escape."""
    return value.encode("ascii", "backslashreplace").decode("ascii")


def _values(entries):
    return " ; ".join(show(e.value) for e in entries)


def describe(report):
    """The human report, as lines."""
    lines = ["watched keys:"]
    for f in report.watched:
        lines.append("  %-16s %s  %s" % (f.verdict, show(f.key),
                                         show(f.message)))
    if report.respelled:
        lines.append("respelled (case folded, so not a drop; whether the "
                     "reader folds case is not documented):")
        for was, now in report.respelled:
            lines.append("  %s -> %s" % (show(was), show(now)))
    count = len(report.only_old) + len(report.only_new) + len(report.differs)
    lines.append("other differences: %d" % count)
    for o in report.only_old:
        lines.append("  only OLD  %s = %s" % (show(o[0].key), _values(o)))
    for n in report.only_new:
        lines.append("  only NEW  %s = %s" % (show(n[0].key), _values(n)))
    for o, n in report.differs:
        lines.append("  differs   %s  OLD=%s | NEW=%s"
                     % (show(n[0].key), _values(o), _values(n)))
    if report.ignored:
        lines.append("ignored (build stamps and --ignore): %s"
                     % ", ".join(show(k) for k in report.ignored))
    lost = [f.key for f in report.watched if f.verdict in FAILING]
    lines.append("")
    if lost:
        lines.append("VERDICT: %s lost or unclear in NEW. Searches that "
                     "relied on a lost setting may stop matching."
                     % show(", ".join(lost)))
    else:
        lines.append("VERDICT: no watched key was lost.")
    return lines


def gate(report):
    return 1 if report.failed else 0


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the core and the CLI. Temp files only, no network."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def diff(old, new, **kw):
        return compare(parse_loc(old), parse_loc(new), **kw)

    def verdict(old, new, **kw):
        return diff(old, new, **kw).watched[0].verdict

    print("locdiff self-test: temp files only, no network, no credentials")
    print("-" * 68)

    # ---- the incident: a rebuild that loses the key
    old = "Category = Address\nsupportsOptionalZone = true\nMinimumMatchScore = 85\n"
    new = "Category = Address\nMinimumMatchScore = 85\n"
    check(verdict(old, new) == DROPPED,
          "a rebuild that loses supportsOptionalZone = true is DROPPED"
          "  <-- pinned defect")
    check(gate(diff(old, new)) == 1, "and the gate fails on it")
    check("OLD line 2 = true" in diff(old, new).watched[0].message,
          "and the finding names the OLD line and value")
    check(verdict(old, old) == SAME and gate(diff(old, old)) == 0,
          "the same text on both sides is SAME and passes")
    check(verdict(old, "supportsOptionalZone = false\n") == CHANGED,
          "true in OLD and false in NEW is CHANGED  <-- pinned defect")
    check(verdict("supportsOptionalZone = false\n",
                  "supportsOptionalZone = true\n") == CHANGED,
          "false to true is CHANGED too: the gate does not pick a direction")
    check(verdict("supportsOptionalZone = false\n", "") == DROPPED_DEFAULT,
          "dropping false, the documented default, is DROPPED-DEFAULT")
    check(gate(diff("supportsOptionalZone = false\n", "")) == 0,
          "and does not fail the gate, because nothing changes")
    check(verdict("", "supportsOptionalZone = true\n") == ADDED,
          "a key only NEW sets is ADDED")
    check(gate(diff("", "supportsOptionalZone = true\n")) == 0,
          "and ADDED does not fail the gate")
    check(verdict("A = 1\n", "A = 1\n") == ABSENT,
          "a key on neither side is ABSENT, not SAME")
    check(gate(diff("A = 1\n", "A = 1\n")) == 0, "and ABSENT passes")

    # ---- the spellings Esri files really use
    check(verdict("supportsOptionalZone = true\n",
                  "SupportsOptionalZone = true\n") == SAME,
          "a case-only respelling is the same key, not a drop"
          "  <-- pinned defect")
    rep = diff("supportsOptionalZone = true\n", "SupportsOptionalZone = true\n")
    check(rep.respelled == [("supportsOptionalZone", "SupportsOptionalZone")],
          "and the respelling is reported")
    check(rep.only_old == [] and rep.only_new == [],
          "and it is not also listed as only OLD and only NEW"
          "  <-- pinned defect")
    check(verdict("supportsOptionalZone = True\n",
                  "supportsOptionalZone = TRUE\n") == SAME,
          "the values True and TRUE are one boolean  <-- pinned defect")
    check(diff("Description = Streets\n", "Description = STREETS\n").differs
          != [], "but a non-boolean value is compared verbatim")
    check(verdict("supportsOptionalZone = true\n",
                  "; supportsOptionalZone = true\n") == DROPPED,
          "a commented-out key counts as absent  <-- pinned defect")
    check(verdict("supportsOptionalZone=true\n", "") == DROPPED,
          "a key written with no spaces round the = is read")
    check(verdict("  supportsOptionalZone = true  \r\n", "") == DROPPED,
          "leading space and a CRLF ending are read")
    check(verdict(BOM + "supportsOptionalZone = true\n", "") == DROPPED,
          "a UTF-8 BOM before the first key is stripped  <-- pinned defect")
    check(parse_line("LocatorWorkspaceString = ProgID = x; Path = y")
          == ("LocatorWorkspaceString", "ProgID = x; Path = y"),
          "only the first = splits, so a value may hold one")
    check(parse_line("BatchOutputFields =") == ("BatchOutputFields", ""),
          "an empty value is a live setting")
    check(parse_line("no equals here") is None, "a line with no = is skipped")
    check(parse_line("") is None, "a blank line is skipped")
    check(parse_line("1st = x") is None and parse_line("a b = x") is None,
          "a key that is not a name is skipped")
    check([e.line for e in parse_loc("\n; c\nA = 1\n\nB = 2")] == [3, 5],
          "lines are numbered from 1, blanks and comments included")
    check(repr(parse_loc("A = 1")[0]) == "Entry(1, 'A', '1')",
          "an entry prints its line, key and value")

    # ---- keys that repeat
    multi_old = "BatchPresortInputs = State\nBatchPresortInputs = ZIP\n"
    multi_new = "BatchPresortInputs = State\n"
    rep = diff(multi_old, multi_new)
    check(len(rep.differs) == 1,
          "a repeated key keeps every line, so losing one of two is seen"
          "  <-- pinned defect")
    check(diff(multi_old, multi_old).differs == [],
          "and the same repeated lines compare equal")
    check(verdict("supportsOptionalZone = true\nsupportsOptionalZone = false\n",
                  "supportsOptionalZone = true\n") == AMBIGUOUS,
          "a watched key set twice with two values in OLD is AMBIGUOUS"
          "  <-- pinned defect")
    rep = diff("supportsOptionalZone = true\n",
               "supportsOptionalZone = true\nSupportsOptionalZone = false\n")
    check(rep.watched[0].verdict == AMBIGUOUS and gate(rep) == 1,
          "two spellings with two values in NEW are AMBIGUOUS and fail")
    check("lines 1, 2" in rep.watched[0].message
          and "NEW" in rep.watched[0].message,
          "and the finding names the side and both lines")
    check(verdict("supportsOptionalZone = true\nsupportsOptionalZone = TRUE\n",
                  "supportsOptionalZone = true\n") == SAME,
          "a key set twice to one value is not ambiguous")

    # ---- what the other differences hold
    stamp_old = ("RebuildStartTime = 1\nRebuildStartTimeAscii = 10:00 AM\n"
                 "MinimumMatchScore = 85\nComprehensiveZoneMatch = true\n")
    stamp_new = ("RebuildStartTime = 2\nRebuildStartTimeAscii = 11:00 AM\n"
                 "MinimumMatchScore = 75\nMaxCandidates = 50\n")
    rep = diff(stamp_old, stamp_new)
    check(rep.ignored == ["RebuildStartTime", "RebuildStartTimeAscii"],
          "the two rebuild time stamps are ignored  <-- pinned defect")
    check([o[0].key for o in rep.only_old] == ["ComprehensiveZoneMatch"],
          "a key only OLD sets is listed as only OLD")
    check([n[0].key for n in rep.only_new] == ["MaxCandidates"],
          "a key only NEW sets is listed as only NEW")
    check([n[0].key for o, n in rep.differs] == ["MinimumMatchScore"],
          "a value that changed is listed as differs")
    check(gate(rep) == 0,
          "other differences are reported but never fail the gate")
    rep = diff(stamp_old, stamp_new, ignore=["minimummatchscore"])
    check(rep.differs == [] and "MinimumMatchScore" in rep.ignored,
          "--ignore adds a key, in any case")
    check(diff("RebuildStartTime = 1\n", "").ignored == ["RebuildStartTime"],
          "a build stamp only one side has is still ignored")
    rep = diff(stamp_old, stamp_new, watch=["ComprehensiveZoneMatch"])
    check(rep.watched[0].verdict == DROPPED and gate(rep) == 1,
          "--watch gates another key")
    check(rep.only_old == [],
          "and a watched key is not listed again under other differences")
    rep = diff(old, new, watch=["supportsOptionalZone", "SUPPORTSOPTIONALZONE"])
    check(len(rep.watched) == 1, "a key watched twice is judged once")
    check(diff("x = 1\n", "x = 1\n", watch=["X"]).watched[0].verdict == SAME,
          "a watched key is looked up with its case folded")
    check(verdict("ComprehensiveZoneMatch = false\n", "",
                  watch=["ComprehensiveZoneMatch"]) == DROPPED,
          "only supportsOptionalZone has a documented default, so another "
          "key dropped at false is DROPPED")

    # ---- the report text
    text = "\n".join(describe(diff(stamp_old + "supportsOptionalZone = true\n",
                                   stamp_new + "supportsoptionalzone = true\n"
                                   + "Description = caf" + chr(0xE9) + "\n")))
    check("SAME             supportsOptionalZone" in text,
          "the report lists every watched key with its verdict")
    check("supportsOptionalZone -> supportsoptionalzone" in text,
          "the report lists a respelling")
    check("only OLD  ComprehensiveZoneMatch = true" in text
          and "only NEW  MaxCandidates = 50" in text
          and "differs   MinimumMatchScore  OLD=85 | NEW=75" in text,
          "the report lists each other difference")
    check("caf\\xe9" in text, "a non-ASCII value prints as an escape, never "
          "crashes a cp1252 console  <-- pinned defect")
    check("ignored (build stamps and --ignore): RebuildStartTime" in text,
          "the report names the keys it ignored")
    check("VERDICT: no watched key was lost." in text,
          "a clean diff says so")
    text = "\n".join(describe(diff(old, new)))
    check("VERDICT: supportsOptionalZone lost or unclear in NEW" in text,
          "a lost key is named in the verdict")
    check("respelled" not in text and "ignored" not in text,
          "empty sections are not printed")
    check("other differences: 0" in text, "and the count of other "
          "differences is printed even at 0")

    # ---- planning and applying the restore
    check(plan_edits(diff(old, new)) == [("supportsOptionalZone", "true")],
          "a DROPPED key plans OLD's own value")
    check(plan_edits(diff(old, "supportsOptionalZone = false\n"))
          == [("supportsOptionalZone", "true")],
          "a CHANGED key plans OLD's value")
    check(plan_edits(diff("supportsOptionalZone = false\n", "")) == [],
          "a DROPPED-DEFAULT key plans nothing: absent already means false")
    check(plan_edits(diff(old, old)) == [], "a SAME key plans nothing")
    raises(lambda: plan_edits(diff(
        "supportsOptionalZone = true\nsupportsOptionalZone = false\n", "")),
        "an AMBIGUOUS key refuses the whole plan  <-- pinned defect")
    edits = [("supportsOptionalZone", "true")]
    check(apply_edits("A = 1\n", edits) == "A = 1\nsupportsOptionalZone = true\n",
          "a missing key is appended")
    check(apply_edits("A = 1\r\nB = 2\r\n", edits)
          == "A = 1\r\nB = 2\r\nsupportsOptionalZone = true\r\n",
          "a CRLF file gets a CRLF line  <-- pinned defect")
    check(apply_edits("A = 1", edits) == "A = 1\nsupportsOptionalZone = true\n",
          "a file with no final newline gets one before the new line")
    check(apply_edits("A = 1\r\nB = 2", edits)
          == "A = 1\r\nB = 2\r\nsupportsOptionalZone = true\r\n",
          "and a CRLF file with no final newline gets a CRLF")
    check(apply_edits("", edits) == "supportsOptionalZone = true\n",
          "an empty file gets the one line")
    check(apply_edits("A = 1\r\nSupportsOptionalZone = false\r\nB = 2\r\n", edits)
          == "A = 1\r\nSupportsOptionalZone = true\r\nB = 2\r\n",
          "a line NEW has is rewritten in place, in NEW's spelling and ending")
    check(apply_edits(BOM + "supportsOptionalZone = false\n", edits)
          == BOM + "supportsOptionalZone = true\n",
          "a rewrite of the first line keeps the BOM  <-- pinned defect")
    check(apply_edits("supportsOptionalZone = false\nX = 1\n"
                      "supportsOptionalZone = false\n", edits)
          == "supportsOptionalZone = true\nX = 1\nsupportsOptionalZone = true\n",
          "every live line of the key is rewritten, so one value remains")
    check(apply_edits("; supportsOptionalZone = false\n", edits)
          == "; supportsOptionalZone = false\nsupportsOptionalZone = true\n",
          "a commented line is left alone and the key is appended")
    check(apply_edits("A = 1\n", []) == "A = 1\n", "no edit changes nothing")
    restored = apply_edits(new, plan_edits(diff(old, new)))
    check(gate(diff(old, restored)) == 0,
          "after the restore the diff passes")

    # ---- the parser
    check(_parse(["a.loc", "b.loc"]).watch is None, "--watch is unset by "
          "default, which means supportsOptionalZone")
    check(_parse(["a", "b", "--watch", "K1", "--watch", "K2"]).watch
          == ["K1", "K2"], "--watch repeats")
    check(_parse(["a", "b", "--ignore", "K"]).ignore == ["K"], "--ignore is read")
    check(_parse(["a", "b", "--apply"]).apply is True, "--apply is read")
    # With abbreviations on, --ap parses as --apply and writes NEW.
    for prefix in ("--ap", "--appl", "--wat", "--self"):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                _parse(["a", "b", prefix])
            except SystemExit:
                pass
        check("unrecognized arguments: %s" % prefix in err.getvalue(),
              "the prefix %s is refused, not expanded  <-- pinned defect"
              % prefix)

    # ---- the CLI, on real temp files
    tmp = tempfile.mkdtemp(prefix="locdiff-")
    old_path = os.path.join(tmp, "old.loc")
    new_path = os.path.join(tmp, "new.loc")
    old_bytes = (b"Category = Address\r\nsupportsOptionalZone = true\r\n"
                 b"RebuildStartTime = 1\r\n")
    new_bytes = b"Category = Address\r\nRebuildStartTime = 2\r\n"

    def put(path, data):
        with open(path, "wb") as handle:
            handle.write(data)

    def get(path):
        with open(path, "rb") as handle:
            return handle.read()

    def run(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    put(old_path, old_bytes)
    put(new_path, new_bytes)
    code, out, _ = run([old_path, new_path])
    check(code == 1, "the CLI exits 1 on the dropped key  <-- pinned defect")
    check("DROPPED          supportsOptionalZone" in out,
          "and prints the finding")
    check("Check only. NEW was not written" in out,
          "and says that without --apply nothing was written")
    check(get(new_path) == new_bytes and get(old_path) == old_bytes,
          "and both files are byte for byte unchanged  <-- pinned defect")
    check(not [n for n in os.listdir(tmp) if n not in ("old.loc", "new.loc")],
          "and no backup or other file was created")
    code, out, _ = run([old_path, new_path, "--apply"])
    backup = new_path + ".bak.1"
    check(code == 0, "--apply restores the key and the re-check exits 0")
    check(get(new_path) == new_bytes + b"supportsOptionalZone = true\r\n",
          "the NEW file gains exactly the one line, in CRLF")
    check(os.path.isfile(backup) and get(backup) == new_bytes,
          "the backup holds NEW as it was  <-- pinned defect")
    check(get(old_path) == old_bytes, "the OLD file is never written")
    check("wrote " in out and "re-check:" in out,
          "the run reports the write and the re-check")
    check(not os.path.exists(new_path + ".locdiff-tmp"),
          "the temp file is gone after the write")
    put(new_path, new_bytes)
    run([old_path, new_path, "--apply"])
    check(get(backup) == new_bytes and os.path.isfile(new_path + ".bak.2"),
          "a second --apply never overwrites the first backup"
          "  <-- pinned defect")
    code, out, _ = run([old_path, new_path, "--apply"])
    check(code == 0 and "nothing to restore" in out
          and not os.path.exists(new_path + ".bak.3"),
          "--apply with nothing to restore writes no backup")
    code, out, _ = run([old_path, new_path])
    check(code == 0 and "Check only" not in out,
          "a clean pair prints no write hint")

    put(new_path, b"supportsOptionalZone = true\nsupportsOptionalZone = false\n")
    code, out, err = run([old_path, new_path])
    check(code == 1 and "AMBIGUOUS" in out and err == ""
          and "Check only" not in out,
          "an AMBIGUOUS key without --apply fails and offers no restore")
    code, out, err = run([old_path, new_path, "--apply"])
    check(code == 1 and "Nothing was written" in err
          and not os.path.exists(new_path + ".bak.3"),
          "--apply on an AMBIGUOUS key exits 1 and writes nothing"
          "  <-- pinned defect")

    stem = os.path.join(tmp, "Streets")
    put(stem + ".loc", new_bytes)
    code, out, _ = run([old_path, stem])
    check(code == 1 and "Streets.loc" in out,
          "a locator path with no .loc reads the .loc beside it")
    code, _, err = run([old_path, old_path])
    check(code == 64 and "same file" in err,
          "one file given as both OLD and NEW is a usage error, never a pass"
          "  <-- pinned defect")
    code, _, err = run([old_path])
    check(code == 64 and "two .loc files" in err, "one file is a usage error")
    code, _, err = run([old_path, os.path.join(tmp, "absent.loc")])
    check(code == 2 and "error:" in err, "a missing file exits 2")
    loz = os.path.join(tmp, "Streets.loz")
    put(loz, b"PK\x03\x04\x00\x00binary")
    code, _, err = run([old_path, loz])
    check(code == 2 and "no key = value line" in err,
          "a file with no setting in it, such as a .loz, exits 2"
          "  <-- pinned defect")

    latin = os.path.join(tmp, "latin.loc")
    latin_bytes = b"Description = caf\xe9\nMinimumMatchScore = 85\n"
    put(latin, latin_bytes)
    code, out, _ = run([old_path, latin, "--apply"])
    check(code == 0 and "caf\\xe9" in out,
          "a file that is not UTF-8 is read as Latin-1")
    check(get(latin) == latin_bytes + b"supportsOptionalZone = true\n",
          "and every byte of it survives the write  <-- pinned defect")
    utf_old = os.path.join(tmp, "utf.loc")
    put(utf_old, ("supportsOptionalZone = " + chr(0x2713) + "\n").encode("utf-8"))
    put(latin, latin_bytes)
    code, _, err = run([utf_old, latin, "--apply"])
    check(code == 2 and "cannot hold" in err and get(latin) == latin_bytes
          and not os.path.exists(latin + ".bak.2"),
          "a value NEW's encoding cannot hold exits 2 before any write")

    put(new_path, new_bytes)
    os.mkdir(new_path + ".locdiff-tmp")
    code, _, err = run([old_path, new_path, "--apply"])
    check(code == 2 and "could not write" in err,
          "a write that fails exits 2, not the 1 a lost key exits")
    check(get(new_path) == new_bytes,
          "and NEW is left as it was  <-- pinned defect")

    code, out, _ = run([old_path, new_path, "--watch", "Category"])
    check(code == 0 and "SAME             Category" in out
          and "supportsOptionalZone" not in out.split("other")[0],
          "--watch replaces the default watch list")
    code, out, _ = run([old_path, new_path, "--ignore", "Category"])
    check("ignored (build stamps and --ignore): Category, RebuildStartTime" in out,
          "--ignore reaches the CLI")

    argv_before = sys.argv
    try:
        sys.argv = ["locdiff.py", old_path, old_path]
        check(run(None)[0] == 64,
              "main with no argv reads the arguments after the program name")
    finally:
        sys.argv = argv_before

    # Importing the module, as a caller or a test runner would, must run
    # nothing: no main, no exit, no output.
    spec = importlib.util.spec_from_file_location("locdiff_probe",
                                                  os.path.abspath(__file__))
    probe_module = importlib.util.module_from_spec(spec)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        spec.loader.exec_module(probe_module)
    check(out.getvalue() == "" and probe_module.DEFAULT_WATCH == DEFAULT_WATCH,
          "importing the module runs nothing")

    shutil.rmtree(tmp, ignore_errors=True)

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass. The probe output is swallowed.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")

    print("-" * 68)
    # One print and no if: a green run then covers every line of the footer.
    print("\n".join(["%d assertions, %d failed"
                     % (passed[0] + len(failed), len(failed))]
                    + ["  FAILED: %s" % label for label in failed]))
    return 1 if failed else 0


# ----------------------------------------------------------------------- cli

def locate(path):
    """The .loc for a locator path given with or without its extension."""
    if not path.lower().endswith(".loc") and os.path.isfile(path + ".loc"):
        return path + ".loc"
    return path


def read_loc(path):
    """(bytes, text, entries, codec). UTF-8 when it decodes, else Latin-1.

    Both decodings round-trip exactly, so a write changes only the lines it
    sets. Raises ValueError for a file with no setting in it.
    """
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        text, codec = raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, codec = raw.decode("latin-1"), "latin-1"
    entries = parse_loc(text)
    if not entries:
        raise ValueError("%s has no key = value line, so it is not a .loc "
                         "file. Pass the .loc beside the .loz." % path)
    return raw, text, entries, codec


def write_loc(path, raw, text, codec):
    """Back up NEW to the first free PATH.bak.N, then replace it.

    The encode runs first, so a value the codec cannot hold stops the run
    before the disk is touched. The backup is opened exclusive, so an earlier
    backup is never overwritten. Returns the backup's path.
    """
    try:
        data = text.encode(codec)
    except UnicodeEncodeError:
        raise ValueError("%s is %s and cannot hold the value to restore. "
                         "Nothing was written." % (path, codec))
    number = 1
    while True:
        backup = "%s.bak.%d" % (path, number)
        try:
            with open(backup, "xb") as handle:
                handle.write(raw)
            break
        except FileExistsError:
            number += 1
    temp = path + ".locdiff-tmp"
    with open(temp, "wb") as handle:
        handle.write(data)
    os.replace(temp, path)
    return backup


def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="locdiff.py",
        description="Diff two ArcGIS locator .loc files and fail when a "
                    "watched setting was dropped or changed.",
        epilog="Nothing is written without --apply. OLD is never written.",
        allow_abbrev=False,
    )
    ap.add_argument("old", nargs="?", metavar="OLD",
                    help="the .loc of the build that worked")
    ap.add_argument("new", nargs="?", metavar="NEW",
                    help="the .loc of the rebuild")
    ap.add_argument("--watch", action="append", metavar="KEY",
                    help="a key whose loss fails the run. Repeat it. Given "
                         "at all, it replaces the default %s." % DEFAULT_WATCH)
    ap.add_argument("--ignore", action="append", default=[], metavar="KEY",
                    help="a key to leave out of the other differences, on "
                         "top of %s" % ", ".join(BUILD_STAMP_KEYS))
    ap.add_argument("--apply", action="store_true",
                    help="copy OLD's value of each dropped or changed watched "
                         "key into NEW, after a backup")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return self_test()
    if not args.old or not args.new:
        print("error: two .loc files are required, OLD then NEW. Use "
              "--self-test to check the tool without them.", file=sys.stderr)
        return 64
    old_path, new_path = locate(args.old), locate(args.new)
    if (os.path.normcase(os.path.realpath(old_path))
            == os.path.normcase(os.path.realpath(new_path))):
        print("error: OLD and NEW are the same file, so the diff could only "
              "pass.", file=sys.stderr)
        return 64
    try:
        old = read_loc(old_path)
        new = read_loc(new_path)
    except (IOError, OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    watch = args.watch or [DEFAULT_WATCH]
    report = compare(old[2], new[2], watch, args.ignore)
    print("locdiff: OLD %s (%d settings), NEW %s (%d settings)"
          % (show(old_path), len(old[2]), show(new_path), len(new[2])))
    for line in describe(report):
        print(line)

    try:
        edits = plan_edits(report)
    except ValueError as exc:
        if args.apply:
            print("error: %s" % exc, file=sys.stderr)
        return gate(report)
    if not args.apply:
        if edits:
            print("\nCheck only. NEW was not written. Re-run with --apply to "
                  "restore %d key(s) from OLD." % len(edits))
        return gate(report)
    if not edits:
        print("\n--apply: nothing to restore, so nothing was written.")
        return gate(report)
    try:
        backup = write_loc(new_path, new[0], apply_edits(new[1], edits),
                           new[3])
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except (IOError, OSError) as exc:
        print("error: could not write %s: %s" % (show(new_path), exc),
              file=sys.stderr)
        return 2
    print("\nwrote %s (backup %s): %s"
          % (show(new_path), show(backup),
             ", ".join("%s = %s" % (show(k), show(v)) for k, v in edits)))
    after = compare(old[2], read_loc(new_path)[2], watch, args.ignore)
    print("re-check:")
    for f in after.watched:
        print("  %-16s %s  %s" % (f.verdict, show(f.key), show(f.message)))
    return gate(after)


if __name__ == "__main__":
    sys.exit(main())
