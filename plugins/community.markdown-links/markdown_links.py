#!/usr/bin/env python3
"""community.markdown-links: audit local links in Markdown files.

Scans an explicit list of workspace Markdown paths (never a recursive walk),
checks inline links/images that point at local files or anchors, and writes
one deterministic JSON report to a new path created with O_EXCL.

Supported syntax subset (documented, not full CommonMark):
  - Inline links [text](target) and images ![alt](target); an optional
    "title" inside the parentheses is ignored.
  - Backtick and tilde fenced code blocks (fences may be indented up to 3
    spaces); link syntax inside fences is ignored.
  - Inline code spans delimited by matching runs of backticks are ignored.
  - ATX headings (# .. ######) produce anchors.
  - URI/email autolinks are counted as external and never fetched.
    Bare URLs are ignored (not counted).
Unsupported constructs (counted, never checked):
  - Reference-style links [text][label] and definitions [label]: target
    are reported as rule "unsupported-construct" once per file.
  - Setext headings (underlined with === or ---) are reported and do not
    produce anchors.
Anchor algorithm (explicit limited algorithm, deterministic):
  slug = lowercase, remove every character that is not alphanumeric,
  underscore, hyphen, space or a non-ASCII letter/digit, then replace each
  remaining space with '-'. Duplicate headings within one document get
  sequential suffixes: slug, slug-1, slug-2, ...

External URLs (any target with a URI scheme such as https:, or starting
with //) are counted and never fetched. No network access is performed.

Exit contract:
  0 - report written at --report (inspect report["status"]).
  2 - argument/path rejection; no report created.
  3 - internal failure; no report created.
Report status is "ok", "issues", or "refused" (input/resource failure);
a file that could not be read never produces a clean result.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

TOOL = "community.markdown-links"
VERSION = "0.1.0"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_INPUTS = 512
MAX_FINDINGS = 1000
MAX_ANCHOR_TARGETS = 256

AUTOLINK = re.compile(r"<(?:(?:[A-Za-z][A-Za-z0-9+.-]*:|//)[^<>\s]+|[^<>\s@]+@[^<>\s@]+)>")
SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
REFERENCE_DEF = re.compile(r"^\s{0,3}\[[^\]\n]+\]:")
ATX_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class Reject(Exception):
    """Argument or path rejection (exit 2, no report written)."""


def workspace_path(raw, label):
    if not isinstance(raw, str) or not raw:
        raise Reject(f"{label} must be a non-empty workspace-relative path")
    if "\x00" in raw:
        raise Reject(f"{label} contains a NUL byte")
    posix = PurePosixPath(raw)
    if posix.is_absolute():
        raise Reject(f"{label} must be relative to the workspace, not absolute")
    if any(part == ".." for part in posix.parts):
        raise Reject(f"{label} may not contain '..'")
    root = Path.cwd().resolve()
    candidate = Path(raw)
    try:
        resolved = candidate.resolve()
    except OSError as error:
        raise Reject(f"{label} cannot be resolved: {error}")
    if not resolved.is_relative_to(root):
        raise Reject(f"{label} resolves outside the workspace: {raw}")
    return candidate, resolved


def slugify(text):
    out = []
    for char in text.strip().lower():
        if char == " ":
            out.append(" ")
        elif char in "-_":
            out.append(char)
        elif char.isalnum() or unicodedata.category(char).startswith("L"):
            out.append(char)
        # every other character (punctuation, symbols) is removed
    return "".join(out).replace(" ", "-")


def headings_and_slugs(text):
    """Return ordered slugs for ATX headings, applying the documented
    duplicate-suffix rule. Caller passes fence-stripped lines."""
    slugs = []
    used = {}
    for line in text.split("\n"):
        match = ATX_HEADING.match(line)
        if not match:
            continue
        base = slugify(match.group(2))
        count = used.get(base, 0)
        used[base] = count + 1
        slugs.append(base if count == 0 else f"{base}-{count}")
    return slugs


def strip_inline_code(line):
    """Remove `code` spans (runs of 1+ backticks matched by an equal run)."""
    result = []
    index = 0
    while index < len(line):
        if line[index] == "`":
            end = index
            while end < len(line) and line[end] == "`":
                end += 1
            run = line[index:end]
            close = line.find(run, end)
            if close == -1:
                result.append(line[index:])
                break
            result.append(" ")
            index = close + len(run)
        else:
            result.append(line[index])
            index += 1
    return "".join(result)


def fenced_lines(text):
    """Yield (line_number, line, in_fence)."""
    in_fence = False
    fence_char = fence_len = None
    for number, line in enumerate(text.split("\n"), start=1):
        match = FENCE.match(line)
        if match and not in_fence:
            in_fence = True
            fence_char = match.group(1)[0]
            fence_len = len(match.group(1))
            yield number, line, True
        elif in_fence:
            if match and match.group(1)[0] == fence_char and \
                    len(match.group(1)) >= fence_len and not line[match.end():].strip():
                in_fence = False
            yield number, line, True
        else:
            yield number, line, False


def read_text_file(resolved, max_bytes=MAX_FILE_BYTES):
    st = os.stat(resolved, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode):
        raise Reject("not a regular file")
    if st.st_size > max_bytes:
        raise Reject(f"exceeds {max_bytes} byte budget")
    with open(resolved, "rb") as handle:
        data = handle.read(st.st_size + 1)
    if len(data) != st.st_size:
        raise Reject("file size changed or incomplete read")
    return data


class Audit:
    def __init__(self):
        self.findings = []
        self.overflow = 0
        self.counts = {"links_checked": 0, "external_skipped": 0,
                       "anchors_checked": 0, "unsupported_constructs": 0,
                       "files_read": 0, "files_failed": 0}
        self.anchor_cache = {}  # resolved path -> set of slugs or None

    def add(self, rule, source, line, target, message):
        if len(self.findings) < MAX_FINDINGS:
            self.findings.append({
                "rule": rule, "source": source, "line": line,
                "target": target, "message": message})
        else:
            self.overflow += 1

    def anchors_for(self, resolved):
        if resolved in self.anchor_cache:
            return self.anchor_cache[resolved]
        if len(self.anchor_cache) >= MAX_ANCHOR_TARGETS:
            self.anchor_cache[resolved] = None
            return None
        try:
            data = read_text_file(resolved)
            text = data.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError, Reject):
            self.anchor_cache[resolved] = None
            return None
        body = "\n".join(line for _, line, fenced
                         in fenced_lines(text) if not fenced)
        slugs = set(headings_and_slugs(body))
        self.anchor_cache[resolved] = slugs
        return slugs

    def check_target(self, source_rel, source_dir, line, raw):
        target = raw.strip()
        if not target:
            return
        if target.startswith("//") or SCHEME.match(target):
            self.counts["external_skipped"] += 1
            return
        path_part, fragment = target, ""
        if "#" in target:
            path_part, fragment = target.split("#", 1)
        try:
            decoded_path = unquote(path_part, errors="strict")
            decoded_frag = unquote(fragment, errors="strict")
        except UnicodeDecodeError:
            self.add("bad-target-encoding", source_rel, line, target,
                     "percent-encoding is malformed")
            return
        if path_part == "":
            # same-document anchor
            resolved = source_dir[1]
            self.counts["links_checked"] += 1
            self.check_anchor(resolved, source_rel, line, target,
                              decoded_frag)
            return
        rel_parts = decoded_path.split("/")
        if decoded_path.startswith("/"):
            joined = PurePosixPath(*[p for p in rel_parts if p])
        else:
            joined = PurePosixPath(source_dir[0]).parent.joinpath(*rel_parts)
        normalized = []
        for part in joined.parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not normalized:
                    self.add("path-escapes-workspace", source_rel, line,
                             target, "link target traverses above the workspace")
                    return
                normalized.pop()
            else:
                normalized.append(part)
        rel_target = PurePosixPath(*normalized) if normalized else PurePosixPath(".")
        resolved = (Path.cwd() / Path(*rel_target.parts)).resolve()
        root = Path.cwd().resolve()
        if not resolved.is_relative_to(root):
            self.add("path-escapes-workspace", source_rel, line, target,
                     "link target resolves outside the workspace")
            return
        self.counts["links_checked"] += 1
        if not resolved.exists():
            self.add("missing-file", source_rel, line, target,
                     "link target does not exist")
            return
        if resolved.is_dir():
            self.add("target-not-file", source_rel, line, target,
                     "link target is a directory")
            return
        if decoded_frag:
            self.check_anchor(resolved, source_rel, line, target, decoded_frag)

    def check_anchor(self, resolved, source_rel, line, target, fragment):
        if fragment == "":
            return
        if resolved.suffix.lower() not in (".md", ".markdown", ".mdown",
                                          ".mkd", ".mdx"):
            return  # anchors only defined for Markdown documents
        self.counts["anchors_checked"] += 1
        slugs = self.anchors_for(resolved)
        if slugs is None:
            self.add("anchor-target-unreadable", source_rel, line, target,
                     "target file could not be read for anchor resolution")
            return
        if fragment.lower() not in slugs:
            self.add("missing-anchor", source_rel, line, target,
                     "no heading produces this anchor")


def bracket_labels(line):
    """Scan top-level balanced labels once, without regex backtracking.

    Unmatched brackets are literal text. Nested labels are recognized only
    so an inline link containing one can be flagged, not partially checked.
    """
    depth = 0
    start = 0
    nested = False
    for index, char in enumerate(line):
        if char == "[":
            if depth == 0:
                start = index
                nested = False
            else:
                nested = True
            depth += 1
        elif char == "]" and depth:
            depth -= 1
            if depth == 0:
                yield start, index + 1, nested


def reference_use_count(line):
    previous = None
    count = 0
    for start, end, nested in bracket_labels(line):
        if previous is not None and previous[1] == start and previous[1] - previous[0] > 2:
            count += 1
            previous = None
        else:
            previous = (start, end)
    return count


def inline_destinations(line):
    """Yield (start, end, target or None). Limited single-line grammar.

    Balanced bare destinations, angle-wrapped destinations, and optional
    quoted/parenthesized titles are supported. Never check a truncated target.
    None marks a link-like construct outside this grammar as unsupported.
    """
    cursor = 0
    for start, label_end, nested in bracket_labels(line):
        if start < cursor or label_end >= len(line) or line[label_end] != "(":
            continue
        if start and line[start - 1] == "!":
            start -= 1
        pos = label_end + 1
        while pos < len(line) and line[pos].isspace():
            pos += 1
        target_start = pos
        if pos < len(line) and line[pos] == "<":
            close = line.find(">", pos + 1)
            if close < 0:
                yield start, len(line), None
                return
            target = line[pos + 1:close]
            pos = close + 1
        else:
            depth = 0
            while pos < len(line):
                char = line[pos]
                if char == "(":
                    depth += 1
                elif char == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif char.isspace():
                    break
                pos += 1
            target = line[target_start:pos]
            if depth:
                yield start, len(line), None
                return
        had_space = pos < len(line) and line[pos].isspace()
        while pos < len(line) and line[pos].isspace():
            pos += 1
        if had_space and pos < len(line) and line[pos] in "\"'(":
            delimiter = ")" if line[pos] == "(" else line[pos]
            close = line.find(delimiter, pos + 1)
            if close < 0:
                yield start, len(line), None
                return
            pos = close + 1
            while pos < len(line) and line[pos].isspace():
                pos += 1
        if pos >= len(line) or line[pos] != ")" or "\\" in target:
            yield start, len(line), None
            return
        cursor = pos + 1
        yield start, cursor, None if nested else target


def audit_file(audit, rel):
    candidate, resolved = workspace_path(rel, "input")
    if candidate.is_symlink() or resolved.is_symlink():
        raise Reject(f"input may not be a symbolic link: {rel}")
    data = read_text_file(resolved)
    audit.counts["files_read"] += 1
    digest = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise Reject(f"invalid UTF-8 in {rel} at byte {error.start}")
    unsupported = 0
    previous = ""
    for number, line, fenced in fenced_lines(text):
        if fenced:
            previous = ""
            continue
        stripped = strip_inline_code(line)
        spans = []
        for start, end, raw in inline_destinations(stripped):
            spans.append((start, end))
            if raw is None:
                unsupported += 1
            else:
                audit.check_target(rel, (rel, resolved), number, raw)
        # Do not count angle-wrapped destinations twice, or reference-looking
        # text inside a parsed inline link as an independent construct.
        remaining = list(stripped)
        for start, end in spans:
            remaining[start:end] = " " * (end - start)
        remaining = "".join(remaining)
        audit.counts["external_skipped"] += len(AUTOLINK.findall(remaining))
        uses = reference_use_count(remaining)
        defs = 1 if REFERENCE_DEF.match(remaining) else 0
        setext = bool(previous.strip() and SETEXT.fullmatch(stripped)
                      and not ATX_HEADING.match(previous)
                      and not SETEXT.fullmatch(previous))
        unsupported += uses + defs + int(setext)
        previous = stripped
    if unsupported:
        audit.counts["unsupported_constructs"] += unsupported
        audit.add("unsupported-construct", rel, 0, "",
                  f"{unsupported} construct(s) outside the supported syntax "
                  "subset (reference links/images, setext headings or "
                  "unsupported inline destinations) were not checked")
    return {"path": rel, "bytes": len(data), "sha256": digest}


def run(argv):
    parser = argparse.ArgumentParser(prog="markdown_links")
    parser.add_argument("--input", action="append", required=True,
                        help="workspace-relative Markdown path (repeatable)")
    parser.add_argument("--report", required=True,
                        help="workspace-relative new report path (must not exist)")
    args = parser.parse_args(argv)

    if len(args.input) > MAX_INPUTS:
        raise Reject(f"too many inputs (limit {MAX_INPUTS})")
    report_candidate, report_resolved = workspace_path(args.report, "report")
    report_candidate = Path.cwd() / report_candidate
    # Check the requested final component, not only its resolved referent.
    # O_EXCL below likewise opens that component and rejects a raced symlink.
    if os.path.lexists(report_candidate):
        raise Reject(f"report already exists or is a symbolic link: {args.report}")
    if report_resolved.exists() or os.path.lexists(report_resolved):
        raise Reject(f"report already exists: {args.report}")
    if not report_resolved.parent.is_dir():
        raise Reject("report parent directory does not exist")

    audit = Audit()
    files = []
    failed = []
    seen = set()
    for rel in args.input:
        if rel in seen:
            continue
        seen.add(rel)
        _, resolved = workspace_path(rel, "input")
        if resolved == report_resolved:
            raise Reject("report path must differ from every input path")
        try:
            if not resolved.exists():
                raise Reject(f"input does not exist: {rel}")
            files.append(audit_file(audit, rel))
        except (Reject, OSError) as error:
            audit.counts["files_failed"] += 1
            failed.append({"path": rel, "error": str(error)})
            audit.add("input-unreadable", rel, 0, "", str(error))

    status = "ok"
    if audit.findings or failed:
        status = "issues"
    if failed:
        status = "refused"  # unread input can never be a clean audit
    report = {
        "tool": TOOL,
        "version": VERSION,
        "status": status,
        "inputs": files,
        "unreadable_inputs": failed,
        "counts": audit.counts,
        "findings": audit.findings,
        "findings_total": len(audit.findings) + audit.overflow,
        "findings_omitted": audit.overflow,
        "syntax_subset": "inline links/images, ATX heading anchors; "
                         "reference-style links and setext headings unsupported",
    }
    body = (json.dumps(report, ensure_ascii=True, indent=2,
                       sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(report_candidate, flags, 0o644)
    except OSError as error:
        raise Reject(f"cannot create report: {error}")
    try:
        view = memoryview(body)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
    except OSError as error:
        try:
            os.close(fd)
            os.unlink(report_candidate)
        except OSError:
            pass
        raise Reject(f"report write failed: {error}")
    os.close(fd)
    return 0


def main():
    try:
        code = run(sys.argv[1:])
    except Reject as error:
        print(json.dumps({"error": str(error), "tool": TOOL}))
        return 2
    except Exception as error:  # noqa: BLE001 - clean failure contract
        print(json.dumps({"error": f"internal failure: {type(error).__name__}",
                          "tool": TOOL}))
        return 3
    return code


if __name__ == "__main__":
    sys.exit(main())
