# community.markdown-links — usage

Audits local links and anchors in explicitly listed Markdown files and
writes one deterministic JSON report. No recursive workspace scan, no
network access, no modification of inputs, never overwrites the report.

## Prerequisites

Python 3.9+ (standard library only). In wecode the script runs via the
`audit.run` capability (`managed_script_start`, runtime `python3`).

## Invocation

```json
{"args": ["--input", "docs/guide.md", "--input", "README.md",
          "--report", "out/links.json"],
 "wait_ms": 30000}
```

- `--input` (required, repeatable): workspace-relative Markdown path.
  At most 512 inputs. Only these files are read; links to other local
  Markdown files additionally read that target's headings for anchor
  checks.
- `--report` (required): new workspace-relative report path; created
  exclusively, must not exist (including dangling symlinks), must differ
  from every input. The workspace must not be concurrently modified by
  an untrusted process; this is not a filesystem sandbox.

## Checked and not checked

Checked: inline links `[text](target)` and images `![alt](target)` whose
target is a local path or `#anchor`, relative to the source file, with
percent-decoding (e.g. `%20` for spaces). Balanced parentheses in bare
destinations and angle-wrapped destinations are supported, with optional
single/double-quoted or parenthesized single-line titles. Link labels are
single-line without nested brackets. Balanced nested inline labels are
reported as unsupported, never checked using an inner partial label.
Unmatched brackets are literal text, scanned in linear time rather than
repeatedly searching each possible opening bracket. Backslash escapes, multiline syntax,
HTML parsing and full CommonMark are not supported. Recognized inline
link starts with an unparseable destination are reported as unsupported
rather than checked using a truncated path. Contents of fenced code blocks
(backtick and tilde fences, up to 3 spaces of indent) and inline `` `code` ``
spans are ignored. Closing fences must use the opening character, at
least the opening run length, and have only whitespace after the run.

Not checked (counted, never fetched): any target with a URI scheme
(`https:`, `mailto:`, …) or starting with `//`, plus angle-wrapped URI
and email autolinks. Inline angle-wrapped destinations are counted only
once. Bare URLs are ignored and are not included in skip counts.

Unsupported (reported as `unsupported-construct`, not counted as
checked): full/collapsed reference-style links `[text][label]`, reference
images `![alt][label]` / `![alt][]`, and their definitions,
and setext (`===`/`---`) headings following a nonempty text line, which
produce no anchors. Shortcut references (`[label]` / `![alt]`) are not
recognized or counted. Unsupported findings are grouped once per file
at line 0; their count records the recognized occurrences.

## Anchor algorithm

For a heading line `# Text Here` the slug is the lowercased text with
every character removed that is not alphanumeric, `_`, `-` or a space
(non-ASCII letters and digits are kept), then each space becomes `-`.
Repeated headings in one document get `-1`, `-2`, … suffixes. Fragments
match slugs case-insensitively after percent-decoding. Anchors are only
checked in Markdown targets (`.md`, `.markdown`, `.mdown`, `.mkd`,
`.mdx`).

## Report contract

Exit 0 means the report exists; check `report.status`:

- `ok` — all inputs read, no findings.
- `issues` — findings recorded (e.g. `missing-file`, `missing-anchor`,
  `path-escapes-workspace`, `target-not-file`, `bad-target-encoding`).
- `refused` — at least one listed input could not be read; an unread file
  can never produce a clean audit.

Each finding has a stable `rule`, the source path, the 1-based line, the
raw target and a message. `findings_total`/`findings_omitted` stay exact
when the 1000-finding display cap is reached.

Exit 2: argument/path rejection (absolute paths, `..`, existing report,
symlinked report, input identical to report). Unreadable or symlinked
inputs produce a refused report. Exit 3: internal failure.

## Troubleshooting

- Many `unsupported-construct` findings: the file uses reference-style
  links; convert them to inline form to have them checked.
- `missing-anchor` on a duplicate heading: remember the `-1` suffix rule.
- Links to directories report `target-not-file`: point at a file.

## Example

`examples/index.md` links to `docs/sub page.md`, an existing heading and
a missing anchor. Try:

```json
{"args": ["--input",
          "plugins/community.markdown-links/examples/index.md",
          "--report", "tmp/links-report.json"]}
```
