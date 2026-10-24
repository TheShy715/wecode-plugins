# community.json-audit — usage

Deterministic JSON / JSONL quality report. Reads one workspace file and
writes one new JSON report. Never modifies the input, never performs
network I/O, never overwrites an existing file.

## Prerequisites

Python 3.9+ (standard library only). In wecode the script runs via the
`audit.run` capability (`managed_script_start`, runtime `python3`).

## Invocation

Capability `community.json-audit:audit.run` input:

```json
{"args": ["--input", "data/records.jsonl", "--mode", "jsonl",
          "--report", "out/audit.json"],
 "wait_ms": 30000}
```

- `--input` (required): workspace-relative UTF-8 input path.
- `--mode` (required): `json` or `jsonl`. Mode is never inferred from the
  file name.
- `--report` (required): workspace-relative path for the report. It must
  not exist; it is created exclusively (O_EXCL). It must differ from the
  input and stay inside the workspace.

The start call returns a process handle; poll until the process exits,
then read the report file.

## JSONL policy

Physical lines are split on `\n`. Whitespace-only lines are blank lines:
counted and skipped, never treated as records. Every non-blank line must
be one complete JSON value; a failing line is recorded with its physical
line number (`record-syntax-error`) and does not abort the audit, but the
report status becomes `issues`. Trailing whitespace on a line is
permitted; text after a complete value is a syntax error. Duplicate-key
findings also include the physical `line`, including duplicates nested in
objects or arrays, so identical paths in different records remain distinct.

## Report contract

Exit 0 means the report file exists. Check `report.status`:

- `ok` — complete input parsed without findings.
- `issues` — syntax errors, duplicate keys or invalid UTF-8 were found.
- `refused` — an input or resource limit was reached; the report covers a
  bounded prefix only and is never a clean audit.

The report records input byte size and SHA-256, structural counts
(objects, arrays, strings, integers, decimals, booleans, nulls, keys,
duplicate keys, max depth) and, in JSONL mode, physical line / blank /
record / record-error counts. Findings carry a stable `rule`, a JSONPath
`path` and a message; complete input values are never copied into the
report.

Numbers keep exact meaning: integer tokens use a tagged `decimal.Decimal`
representation and are counted as integers, without Python's integer
string-conversion digit limit. Decimal/exponent tokens are counted as decimals.
Construction preserves all digits regardless of decimal context precision;
no numbers pass through binary floats, and `NaN`/`Infinity` literals are rejected. Exact numeric diagnostics are serialized as
strings where needed (report fields are integers or strings only).

## Errors

- Exit 2: argument/path rejection — missing input, non-regular input,
  absolute/`..` paths, workspace escapes, symlinked inputs, or a report
  path that already exists. No report is created.
- Exit 3: internal failure. No report is created.

## Troubleshooting

- `report already exists`: choose a fresh path; reports are never
  overwritten.
- `status: refused` with `input exceeds ...`: the file is over the 64 MiB
  budget; split it and audit parts explicitly.
- A report that only shows `invalid-utf8` means the file is not valid
  UTF-8; convert it first (e.g. with `iconv`).

## Example

`examples/records.jsonl` contains valid lines, a blank line, one broken
line and a duplicate key. Try:

```json
{"args": ["--input", "plugins/community.json-audit/examples/records.jsonl",
          "--mode", "jsonl", "--report", "tmp/jsonl-report.json"]}
```
