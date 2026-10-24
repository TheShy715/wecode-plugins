#!/usr/bin/env python3
"""community.json-audit: JSON / JSONL quality report.

Reads one workspace file, parses it strictly (no lossy float conversion, no
silent duplicate-key dropping, no non-finite constants), and writes a single
deterministic JSON report to a new path created with O_EXCL.

Exit contract:
  0 - a report file was written at --report (inspect report["status"]).
  2 - argument or path rejection; no report was created.
  3 - internal failure; no report was created.

Report statuses:
  "ok"      - the complete input parsed without findings.
  "issues"  - findings (syntax errors, duplicate keys) were recorded.
  "refused" - a configured resource limit was hit; the report describes a
              bounded prefix only and is never a clean audit.

Numbers keep exact meaning: integer tokens use a tagged decimal.Decimal,
as do decimal numbers; nothing is converted through binary floats or
subject to Python's int string-conversion digit limit.
NaN/Infinity literals are rejected as invalid JSON data. Exact numeric
values are never copied into the report - only counts and paths.

The tool never rewrites the input and never appends to existing files.
"""
import argparse
import decimal
import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath

TOOL = "community.json-audit"
VERSION = "0.1.0"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_DEPTH = 256
MAX_FINDINGS = 200
MAX_NODES = 5_000_000


class Reject(Exception):
    """Argument or path rejection (exit 2, no report written)."""


class Refused(Exception):
    """Resource limit hit while auditing; recorded in the report."""


class JsonInteger(decimal.Decimal):
    """Exact integer token, distinguished from fractional/exponent tokens.

    Decimal construction from text is exact regardless of context precision
    and avoids Python's decimal-string-to-int conversion resource limit.
    """


class JObject:
    """Parsed JSON object preserving every member pair, including
    duplicate keys, so the path-aware walk can report each duplicate at
    its real key path instead of silently merging."""
    __slots__ = ("pairs",)

    def __init__(self, pairs):
        self.pairs = pairs


def workspace_path(raw, must_be, label):
    """Resolve a workspace-relative path argument. Reject absolute paths,
    '..' escapes, and anything that resolves outside the workspace root."""
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
    if candidate.is_symlink():
        raise Reject(f"{label} may not be a symbolic link")
    if must_be == "input":
        if not resolved.exists():
            raise Reject(f"{label} does not exist: {raw}")
        if resolved.is_symlink():
            raise Reject(f"{label} may not resolve through a symbolic link")
        try:
            mode = os.stat(resolved, follow_symlinks=False).st_mode
        except OSError as error:
            raise Reject(f"{label} is not readable: {error}")
        if not stat.S_ISREG(mode):
            raise Reject(f"{label} is not a regular file: {raw}")
    elif must_be == "report":
        if resolved.exists() or os.path.lexists(resolved):
            raise Reject(f"{label} already exists: {raw}")
        if not resolved.parent.is_dir():
            raise Reject(f"{label} parent directory does not exist: {raw}")
    return resolved


def read_input(resolved):
    """Fully read a regular file, checking for short reads / mid-read
    truncation so an incomplete read is never audited silently."""
    try:
        st = os.stat(resolved, follow_symlinks=False)
    except OSError as error:
        raise Reject(f"input is not readable: {error}")
    size = st.st_size
    if size > MAX_INPUT_BYTES:
        raise Refused(f"input exceeds {MAX_INPUT_BYTES} byte budget ({size} bytes)")
    chunks = []
    received = 0
    try:
        with open(resolved, "rb") as handle:
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
    except OSError as error:
        raise Reject(f"input read failed: {error}")
    if received != size:
        raise Refused(
            f"incomplete read: expected {size} bytes, received {received}")
    data = b"".join(chunks)
    return data, hashlib.sha256(data).hexdigest()


def invalid_constant(value):
    raise ValueError(f"non-finite number is not valid JSON data: {value}")


class Auditor:
    """Walks parsed JSON/JSONL values preserving exact numbers and
    reporting duplicate object keys at every depth."""

    def __init__(self, findings):
        self.findings = findings
        self.overflow = 0
        self.nodes = 0
        self.stack = []          # path components (str keys / int indexes)
        self.counts = {
            "objects": 0, "arrays": 0, "strings": 0,
            "integers": 0, "decimals": 0, "booleans": 0, "nulls": 0,
            "object_keys": 0, "duplicate_keys": 0,
        }
        self.max_depth = 0

    def path(self):
        text = "$"
        for part in self.stack:
            if isinstance(part, int):
                text += f"[{part}]"
            else:
                text += "." + json.dumps(part, ensure_ascii=True)
        return text

    def add_finding(self, rule, message, line=None):
        if len(self.findings) < MAX_FINDINGS:
            finding = {"rule": rule, "message": message, "path": self.path()}
            if line is not None:
                finding["line"] = line
            self.findings.append(finding)
        else:
            self.overflow += 1

    def enter(self):
        depth = len(self.stack) + 1
        if depth > self.max_depth:
            self.max_depth = depth
        if depth > MAX_DEPTH:
            raise Refused(f"nesting depth exceeds limit {MAX_DEPTH}")
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise Refused(f"node count exceeds limit {MAX_NODES}")

    def walk(self, value, line=None):
        self.enter()
        if isinstance(value, JObject):
            self.counts["objects"] += 1
            seen = set()
            for key, _ in value.pairs:
                self.counts["object_keys"] += 1
                if key in seen:
                    self.counts["duplicate_keys"] += 1
                    self.stack.append(key)
                    self.add_finding(
                        "duplicate-key",
                        "object key repeated at this path; both values were "
                        "counted, no value was dropped silently", line=line)
                    self.stack.pop()
                else:
                    seen.add(key)
            for key, item in value.pairs:
                self.stack.append(key)
                self.walk(item, line=line)
                self.stack.pop()
        elif isinstance(value, list):
            self.counts["arrays"] += 1
            for index, item in enumerate(value):
                self.stack.append(index)
                self.walk(item, line=line)
                self.stack.pop()
        elif isinstance(value, bool):
            self.counts["booleans"] += 1
        elif value is None:
            self.counts["nulls"] += 1
        elif isinstance(value, (int, JsonInteger)):
            self.counts["integers"] += 1
        elif isinstance(value, decimal.Decimal):
            self.counts["decimals"] += 1
        elif isinstance(value, str):
            self.counts["strings"] += 1

    def parse(self, text):
        self.stack = []
        return json.loads(
            text,
            object_pairs_hook=JObject,
            parse_float=decimal.Decimal,
            parse_int=JsonInteger,
            parse_constant=invalid_constant)


def audit_json(auditor, text):
    try:
        value = auditor.parse(text)
    except json.JSONDecodeError as error:
        auditor.add_finding(
            "syntax-error",
            f"{error.msg} at line {error.lineno} column {error.colno} "
            f"(char {error.pos})")
        return
    except ValueError as error:
        auditor.add_finding("syntax-error", str(error))
        return
    auditor.walk(value)


def audit_jsonl(auditor, raw_bytes):
    """JSONL policy: physical lines are split on b'\\n'. Lines containing
    only whitespace are blank lines - counted and skipped, never records.
    Every non-blank line must be one complete JSON value; a failing line is
    recorded with its physical line number and does not abort the audit,
    but always produces status 'issues'."""
    try:
        text_all = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        auditor.add_finding(
            "invalid-utf8",
            f"bytes {error.start}-{error.end}: {error.reason}")
        return {"physical_lines": None, "blank_lines": 0, "records": 0,
                "record_errors": 0}
    lines = text_all.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # trailing newline terminates the last line, not a new one
    blank = records = record_errors = 0
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            blank += 1
            continue
        records += 1
        auditor.stack = []
        try:
            value = auditor.parse(line)
        except json.JSONDecodeError as error:
            record_errors += 1
            auditor.add_finding(
                "record-syntax-error",
                f"line {number}: {error.msg} at column {error.colno}",
                line=number)
            continue
        except ValueError as error:
            record_errors += 1
            auditor.add_finding("record-syntax-error",
                                f"line {number}: {error}", line=number)
            continue
        auditor.walk(value, line=number)
    return {"physical_lines": len(lines), "blank_lines": blank,
            "records": records, "record_errors": record_errors}


def write_report(resolved, report):
    body = (json.dumps(report, ensure_ascii=True, indent=2,
                       sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(resolved, flags, 0o644)
    except FileExistsError:
        raise Reject("report path appeared during write (already exists)")
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
            os.unlink(resolved)
        except OSError:
            pass
        raise Reject(f"report write failed: {error}")
    os.close(fd)


def run(argv):
    parser = argparse.ArgumentParser(prog="json_audit")
    parser.add_argument("--input", required=True,
                        help="workspace-relative UTF-8 input path")
    parser.add_argument("--mode", required=True, choices=["json", "jsonl"])
    parser.add_argument("--report", required=True,
                        help="workspace-relative new report path (must not exist)")
    args = parser.parse_args(argv)

    input_path = workspace_path(args.input, "input", "input")
    report_path = workspace_path(args.report, "report", "report")
    if input_path == report_path:
        raise Reject("input and report must be different paths")

    findings = []
    auditor = Auditor(findings)
    report = {
        "tool": TOOL,
        "version": VERSION,
        "input": {"path": args.input, "mode": args.mode},
        "limits": {"max_input_bytes": MAX_INPUT_BYTES,
                   "max_depth": MAX_DEPTH,
                   "max_findings": MAX_FINDINGS,
                   "max_nodes": MAX_NODES},
        "findings": findings,
        "findings_total": 0,
        "findings_omitted": 0,
        "status": "ok",
    }
    try:
        data, digest = read_input(input_path)
    except Refused as error:
        report["status"] = "refused"
        report["refusal"] = str(error)
        write_report(report_path, report)
        return 0
    report["input"]["bytes"] = len(data)
    report["input"]["sha256"] = digest

    try:
        if args.mode == "json":
            try:
                text = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                auditor.add_finding(
                    "invalid-utf8",
                    f"bytes {error.start}-{error.end}: {error.reason}")
            else:
                audit_json(auditor, text)
        else:
            report["jsonl"] = audit_jsonl(auditor, data)
    except Refused as error:
        report["status"] = "refused"
        report["refusal"] = str(error)
    report["structure"] = auditor.counts
    report["structure"]["max_depth"] = auditor.max_depth

    if report["status"] == "ok" and findings:
        report["status"] = "issues"
    report["findings_omitted"] = auditor.overflow
    report["findings_total"] = len(findings) + auditor.overflow
    write_report(report_path, report)
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
