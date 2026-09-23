#!/usr/bin/env python3
"""bandaid -- catch symptom-suppression patches in a diff.

Scans only the lines your diff ADDS, looking for signatures of fixes that
make the error disappear instead of removing its cause:

    swallowed exceptions   except: pass / catch (e) {} / .catch(() => {})
    removed guards         raise/assert replaced by a quiet early exit
    broadened catches      except ValueError:  ->  except Exception:
    silenced checkers      # type: ignore / eslint-disable added in a fix
    disabled failures      || true / continue-on-error: true / set +e
    switched-off tests     test.skip( / xit( / @pytest.mark.skip
    timing band-aids       time.sleep( / retries= / await new Promise(setTimeout)

Exit codes: 0 clean, 1 bandaids found, 2 usage error.
Justify an intentional one on the added line itself:

    # bandaid: allow -- legacy API contract, issue #42

Python 3.8+, no dependencies. Input: git diff (default), --patch FILE,
or --patch - for stdin.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

ESCAPE = re.compile(r"(?://|#)\s*bandaid\s*:\s*allow[^\n]*$")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

LINE_RULES = [
    (re.compile(r"^\s*except\s*:\s*$"), "bare-except"),
    (re.compile(r"\bcatch\b\s*(?:\([^)]*\))?\s*\{\s*\}"), "empty-catch"),
    (re.compile(r"\.catch\(\s*(?:\([^)]*\)|function\s*\([^)]*\))\s*=>\s*(?:\{\s*\}|null|undefined|false)\s*\)"
                r"|\.catch\(\s*function\s*\([^)]*\)\s*\{\s*\}\s*\)"), "catch-noop"),
    (re.compile(r"\bif\s+err\s*!=\s*nil\s*\{\s*return\s+nil\s*\}?\s*$"), "go-err-drop"),
    (re.compile(r"(?://|#)\s*(?:eslint-disable(?:-next-line)?|biome-ignore|@?ts-(?:ignore|expect-error|nocheck)"
                r"|type:\s*ignore|noqa|pylint:\s*disable|nosec|nolint|NOPMD|NOSONAR)"), "checker-silenced"),
    (re.compile(r"\bas\s+any\b"), "ts-any"),
    (re.compile(r"\b(?:it|test|describe)\.skip\s*\(|\bx(?:it|describe)\s*\(|\bskipTest\s*\("
                r"|@pytest\.mark\.(?:skip(?:if)?|xfail)|\b@(?:Ignore|Disabled)\b|\bfit\s*\(|\bfdescribe\s*\("),
     "test-off"),
    (re.compile(r"continue-on-error:\s*true|\|\|\s*true\s*$|^\s*set\s\+e\b|allow_failure:\s*true"),
     "failure-hidden"),
    (re.compile(r"contextlib\.suppress|\bsuppress\s*\("), "suppress"),
    (re.compile(r"\btime\.sleep\s*\(\s*\d|\bmax_retries\s*=|\bretries\s*=|\bmax_attempts\s*="
                r"|\bretry\s*\(\s*[a-zA-Z_]|\bawait\s+new\s+Promise\s*\(\s*\w+\s*=>\s*setTimeout"),
     "sleep-retry"),
]

EXCEPT_HEAD = re.compile(r"^\s*except\b[^:]*:")
SWALLOW_NEXT = re.compile(r"^\s*(?:pass|continue)\b")
SILENT_EXIT_NEXT = re.compile(r"^\s*(?:return|break)\b")
GO_ERR_HEAD = re.compile(r"^\s*if\s+err\s*!=\s*nil\s*\{\s*$")
GO_ERR_NEXT = re.compile(r"^\s*return\s+nil\s*\}?\s*$")

GUARD_SRC = re.compile(r"\b(?:assert|raise|throw)\b")
EARLY_EXIT = re.compile(r"^\s*(?:return|pass|continue|break)\b")
EXCEPT_SPECIFIC = re.compile(r"^\s*except\s+\(?[A-Za-z_]")
EXCEPT_BROAD = re.compile(r"^\s*except\s+Exception\b")

RULES = {
    "bare-except":     ("bandaid", "bare except catches every error, including the bug you are fixing",
                        "catch the specific exception; at minimum `except Exception:` with logging and re-raise"),
    "except-pass":     ("bandaid", "the exception is swallowed: the failure vanishes without being handled",
                        "handle it or re-raise; never make the error invisible"),
    "except-exit":     ("suspect", "control leaves the except block silently: callers cannot tell it failed",
                        "re-raise, or return a result type that carries the error"),
    "empty-catch":     ("bandaid", "empty catch block swallows the failure",
                        "log with the cause and rethrow"),
    "catch-noop":      ("bandaid", "rejected promise is discarded before anyone sees it",
                        "handle the rejection or rethrow"),
    "go-err-drop":     ("bandaid", "error checked, then thrown away: `return nil` hides the failure",
                        "return the error to the caller"),
    "guard-removed":   ("bandaid", "a raise/assert that surfaced this bug was replaced by a quiet exit",
                        "keep the guard; fix whatever violated it"),
    "catch-broadened": ("bandaid", "catch broadened: the original error type no longer reaches the handler",
                        "catch the specific type you actually handle"),
    "checker-silenced": ("suspect", "an added ignore comment silences the checker, not the bug",
                         "fix the finding, or justify with # bandaid: allow -- <reason>"),
    "ts-any":          ("suspect", "type escape hatch added in a fix: the wrong type is a symptom",
                        "type it properly"),
    "test-off":        ("bandaid", "the test that would catch this regression is disabled",
                        "fix the test; if it must be off, add # bandaid: allow -- <reason>"),
    "failure-hidden":  ("bandaid", "a shell/CI step can no longer fail: red turns green by decree",
                        "let the step fail and fix the command"),
    "suppress":        ("suspect", "errors suppressed by policy at the call site",
                        "handle the specific errors you expect"),
    "sleep-retry":     ("suspect", "timing band-aid: sleep/retry papers over ordering or flakiness",
                        "wait on the actual condition, not a duration"),
}

@dataclass
class Finding:
    path: str
    line: int
    code: str
    rule: str

    @property
    def severity(self):
        return RULES[self.rule][0]


def parse_diff(text):
    """Return [(path, added[(lineno, text)], removed[text])] per hunk."""
    hunks = []
    path = None
    cur = None
    newno = 0

    def flush():
        if cur is not None and path and (cur[0] or cur[1]):
            hunks.append((path, cur[0], cur[1]))

    for line in text.splitlines():
        if line.startswith("+++ "):
            flush()
            cur = None
            p = line[4:].strip()
            if p == "/dev/null":
                path = None
            elif p.startswith("b/") or p.startswith("a/"):
                path = p[2:]
            else:
                path = p
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("@@"):
            flush()
            m = HUNK_RE.match(line)
            newno = int(m.group(1)) if m else 1
            cur = ([], [])
            continue
        if cur is None:
            continue
        if line.startswith("+"):
            cur[0].append((newno, line[1:]))
            newno += 1
        elif line.startswith("-"):
            cur[1].append(line[1:])
        elif line == "":
            newno += 1
        elif line.startswith("\\"):
            pass
        elif line.startswith(" "):
            newno += 1
    flush()
    return hunks


def scan_hunk(path, added, removed, stats):
    """Evaluate one hunk; return findings. Escape-justified hits go to stats."""
    findings = []

    def hit(ln, txt, rule):
        if ESCAPE.search(txt):
            stats["suppressed"] += 1
            return
        findings.append(Finding(path, ln, txt, rule))

    bases = []
    for ln, txt in added:
        if ESCAPE.search(txt):
            base = ESCAPE.sub("", txt)
        else:
            base = txt
        bases.append((ln, txt, base))

    # line-level rules run on the escape-stripped base
    for ln, txt, base in bases:
        for rx, rule in LINE_RULES:
            if rx.search(base):
                hit(ln, txt, rule)

    # adjacency rules: head line + next added line with consecutive lineno
    for i, (ln, txt, base) in enumerate(bases):
        if i + 1 >= len(bases) or bases[i + 1][0] != ln + 1:
            continue
        nxt = bases[i + 1][2]
        if EXCEPT_HEAD.match(base):
            if SWALLOW_NEXT.match(nxt):
                hit(ln, txt, "except-pass")
            elif SILENT_EXIT_NEXT.match(nxt):
                hit(ln, txt, "except-exit")
        elif GO_ERR_HEAD.match(base) and GO_ERR_NEXT.match(nxt):
            hit(ln, txt, "go-err-drop")

    # pairing rules: something REMOVED x something ADDED in the same hunk
    if removed and added:
        added_keeps_guard = any(GUARD_SRC.search(base) for _, _, base in bases)
        if any(GUARD_SRC.search(r) for r in removed) and not added_keeps_guard:
            for ln, txt, base in bases:
                if EARLY_EXIT.match(base):
                    hit(ln, txt, "guard-removed")
                    break
        if any(EXCEPT_SPECIFIC.match(r) for r in removed):
            for ln, txt, base in bases:
                if EXCEPT_BROAD.match(base):
                    hit(ln, txt, "catch-broadened")
                    break
    return findings


def scan(diff_text):
    hunks = parse_diff(diff_text)
    findings = []
    stats = {"suppressed": 0, "added_lines": 0, "files": set()}
    for path, added, removed in hunks:
        stats["added_lines"] += len(added)
        stats["files"].add(path)
        findings.extend(scan_hunk(path, added, removed, stats))
    findings.sort(key=lambda f: (f.path, f.line, f.rule))
    return findings, stats


def get_diff(args):
    if args.patch == "-":
        return sys.stdin.read()
    if args.patch:
        try:
            with open(args.patch, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError as exc:
            print("bandaid: cannot read patch: %s" % exc, file=sys.stderr)
            sys.exit(2)
    cmd = ["git", "diff", "--no-color"]
    if args.staged:
        cmd.append("--cached")
    elif args.base:
        cmd.append("%s...HEAD" % args.base)
    else:
        cmd.append("HEAD")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("bandaid: git not found on PATH", file=sys.stderr)
        sys.exit(2)
    if proc.returncode != 0:
        print("bandaid: %s" % (proc.stderr.strip() or "git diff failed"), file=sys.stderr)
        print("bandaid: hint: no commits yet? try --staged, or pass a patch via --patch",
              file=sys.stderr)
        sys.exit(2)
    return proc.stdout

def render_text(findings, stats, counts, strict, color):
    red, yellow, dim, bold, reset = "", "", "", "", ""
    if color:
        red, yellow, dim, bold, reset = "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
    sev_color = {"bandaid": red, "suspect": yellow}
    out = []
    by_file = {}
    for f in findings:
        by_file.setdefault(f.path, []).append(f)
    for path, items in by_file.items():
        out.append("%s%s%s" % (bold, path, reset))
        for f in items:
            msg, sug = RULES[f.rule][1], RULES[f.rule][2]
            out.append("  L%-4d %s%-7s%s %-17s %s" % (
                f.line, sev_color[f.severity], f.severity.upper(), reset, f.rule, msg))
            out.append("         %s+ %s%s" % (dim, f.code.strip(), reset))
            out.append("         %s-> %s%s" % (dim, sug, reset))
        out.append("")
    if not findings:
        out.append("bandaid: clean -- scanned %d added lines across %d files, 0 findings"
                   % (stats["added_lines"], len(stats["files"])))
        if stats["suppressed"]:
            out.append("bandaid: %d justified by 'bandaid: allow'" % stats["suppressed"])
        return "\n".join(out)
    out.append("bandaid: %d bandaids, %d suspects across %d files (%d suppressed by 'bandaid: allow')"
               % (counts["bandaid"], counts["suspect"], len(by_file), stats["suppressed"]))
    failing = counts["bandaid"] > 0 or (strict and counts["suspect"] > 0)
    if failing:
        out.append("bandaid: fix the cause, or justify an intentional line with:  "
                   "# bandaid: allow -- <reason>")
    elif counts["suspect"]:
        out.append("bandaid: suspects alone pass; use --strict to fail on them too")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="bandaid",
        description="Catch symptom-suppression patches (swallowed errors, disabled tests, "
                    "removed guards) in a diff. Exit 1 when bandaids are found.")
    ap.add_argument("--patch", metavar="FILE",
                    help="scan a patch file instead of git; use - for stdin")
    ap.add_argument("--staged", action="store_true", help="scan staged changes (git diff --cached)")
    ap.add_argument("--base", metavar="REF", help="scan changes on this branch (git diff REF...HEAD)")
    ap.add_argument("--strict", action="store_true", help="also fail on suspects")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    args = ap.parse_args(argv)

    if args.staged and args.base:
        ap.error("--staged and --base are mutually exclusive")

    diff_text = get_diff(args)
    findings, stats = scan(diff_text)
    counts = {"bandaid": 0, "suspect": 0}
    for f in findings:
        counts[f.severity] += 1

    failing = counts["bandaid"] > 0 or (args.strict and counts["suspect"] > 0)

    if args.format == "json":
        payload = {
            "ok": not failing,
            "counts": dict(counts, suppressed=stats["suppressed"]),
            "scanned": {"added_lines": stats["added_lines"], "files": sorted(stats["files"])},
            "findings": [
                {"file": f.path, "line": f.line, "rule": f.rule,
                 "severity": f.severity, "code": f.code.strip(),
                 "message": RULES[f.rule][1], "suggestion": RULES[f.rule][2]}
                for f in findings
            ],
        }
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        color = (not args.no_color and sys.stdout.isatty()
                 and not os.environ.get("NO_COLOR"))
        print(render_text(findings, stats, counts, args.strict, color))

    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
