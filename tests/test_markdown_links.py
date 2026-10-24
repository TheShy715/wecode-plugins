import json
import importlib.util
import io
import os
from unittest import mock
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "plugins/community.markdown-links/markdown_links.py"


spec = importlib.util.spec_from_file_location("markdown_links_candidate", SCRIPT)
MODULE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MODULE)


def run_audit(root, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=root, capture_output=True, text=True, timeout=60)


class MarkdownLinksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            data = data.encode("utf-8")
        path.write_bytes(data)

    def report(self, rel="report.json"):
        return json.loads((self.root / rel).read_text("utf-8"))

    def rules(self, rep):
        return [f["rule"] for f in rep["findings"]]

    def test_all_good(self):
        self.write("a.md", "# Title\n[ok](b.md) and [anchor](#title)\n")
        self.write("b.md", "# B\n## Head One\n## Head One\n[x](#head-one-1)\n")
        r = run_audit(self.root, "--input", "a.md", "--input", "b.md",
                      "--report", "report.json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rep = self.report()
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["counts"]["links_checked"], 3)
        self.assertEqual(rep["counts"]["anchors_checked"], 2)

    def test_missing_file_and_anchor(self):
        self.write("a.md",
                   "# T\n[bad](nope.md)\n[anchor](#missing)\n"
                   "[file-anchor](b.md#gone)\n")
        self.write("b.md", "# Real\n")
        r = run_audit(self.root, "--input", "a.md", "--report", "r.json")
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "issues")
        rules = self.rules(rep)
        self.assertIn("missing-file", rules)
        self.assertEqual(rules.count("missing-anchor"), 2)
        finding = rep["findings"][1]
        self.assertEqual(finding["source"], "a.md")
        self.assertEqual(finding["line"], 3)

    def test_external_skipped(self):
        self.write("a.md", "[x](https://example.com) [y](//cdn.com/x) "
                           "[z](mailto:a@b.c) <https://auto>\n")
        r = run_audit(self.root, "--input", "a.md", "--report", "r.json")
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["counts"]["external_skipped"], 4)
        self.assertEqual(rep["counts"]["links_checked"], 0)

    def test_fences_and_inline_code_ignored(self):
        self.write("a.md",
                   "```markdown\n[x](missing.md)\n```\n"
                   "~~~\n[y](missing2.md)\n~~~\n"
                   "`[z](missing3.md)` real text\n"
                   "`` [w](missing4.md) `` double-tick span\n")
        r = run_audit(self.root, "--input", "a.md", "--report", "r.json")
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["counts"]["links_checked"], 0)

    def test_percent_encoded_and_relative(self):
        self.write("docs/sub dir/page.md", "# Page\n")
        self.write("docs/guide.md", "[p](sub%20dir/page.md)\n"
                                  "[up](../root.md)\n")
        self.write("root.md", "# R\n")
        r = run_audit(self.root, "--input", "docs/guide.md",
                      "--report", "r.json")
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "ok", rep["findings"])
        self.assertEqual(rep["counts"]["links_checked"], 2)

    def test_traversal_rejected_as_finding(self):
        self.write("a.md", "[x](../outside.md)\n")
        r = run_audit(self.root, "--input", "a.md", "--report", "r.json")
        rep = self.report("r.json")
        self.assertIn("path-escapes-workspace", self.rules(rep))

    def test_symlink_escape_reported(self):
        outside = self.root.parent / "outside.md"
        outside.write_text("# out\n")
        self.addCleanup(outside.unlink)
        self.write("a.md", "[x](link.md)\n")
        (self.root / "link.md").symlink_to(outside)
        r = run_audit(self.root, "--input", "a.md", "--report", "r.json")
        rep = self.report("r.json")
        self.assertIn("path-escapes-workspace", self.rules(rep))

    def test_unsupported_reference_links(self):
        self.write("a.md", "[x][ref]\n\n[ref]: b.md\n")
        self.write("b.md", "# B\n")
        r = run_audit(self.root, "--input", "a.md", "--report", "r.json")
        rep = self.report("r.json")
        self.assertIn("unsupported-construct", self.rules(rep))
        self.assertEqual(rep["counts"]["unsupported_constructs"], 2)
        self.assertEqual(rep["counts"]["links_checked"], 0)

    def test_unreadable_input_is_refused(self):
        self.write("a.md", "[x](b.md)\n")
        self.write("b.md", "# B\n")
        r = run_audit(self.root, "--input", "a.md", "--input", "missing.md",
                      "--report", "r.json")
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "refused")
        self.assertEqual(rep["counts"]["files_failed"], 1)
        self.assertIn("input-unreadable", self.rules(rep))

    def test_invalid_utf8_input(self):
        self.write("bad.md", b"# T\xff\n")
        r = run_audit(self.root, "--input", "bad.md", "--report", "r.json")
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "refused")

    def test_report_conflicts(self):
        self.write("a.md", "# A\n")
        self.write("r.json", "x")
        r = run_audit(self.root, "--input", "a.md", "--report", "r.json")
        self.assertEqual(r.returncode, 2)
        r = run_audit(self.root, "--input", "a.md", "--report", "a.md")
        self.assertEqual(r.returncode, 2)
        r = run_audit(self.root, "--input", "../x.md", "--report", "r2.json")
        self.assertEqual(r.returncode, 2)

    def test_image_links_and_directory_target(self):
        self.write("a.md", "![img](pics/pic.png)\n[dir](pics)\n")
        self.write("pics/pic.png", "binary")
        r = run_audit(self.root, "--input", "a.md", "--report", "r.json")
        rep = self.report("r.json")
        rules = self.rules(rep)
        self.assertIn("target-not-file", rules)
        self.assertEqual(rules.count("missing-file"), 0)

    def test_heading_slug_algorithm(self):
        self.write("a.md",
                   "# Hello, World!\n"
                   "# CJK 标题\n"
                   "## Foo_Bar-9\n"
                   "[1](#hello-world) [2](#cjk-标题) [3](#foo_bar-9)\n"
                   "[bad](#hello--world)\n")
        r = run_audit(self.root, "--input", "a.md", "--report", "r.json")
        rep = self.report("r.json")
        self.assertEqual(self.rules(rep), ["missing-anchor"])

    def test_dangling_report_symlink_rejected(self):
        self.write("a.md", "# A\n")
        (self.root / "report.json").symlink_to("absent.json")
        result = run_audit(self.root, "--input", "a.md", "--report", "report.json")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertTrue((self.root / "report.json").is_symlink())
        self.assertFalse((self.root / "absent.json").exists())

    def test_raced_final_report_symlink_rejected(self):
        self.write("a.md", "# A\n")
        real_open = os.open
        def raced_open(path, flags, mode):
            (self.root / "report.json").symlink_to("absent.json")
            return real_open(self.root / path, flags, mode)
        with mock.patch("os.getcwd", return_value=str(self.root)), \
                mock.patch.object(MODULE.os, "open", side_effect=raced_open):
            with self.assertRaises(MODULE.Reject):
                MODULE.run(["--input", "a.md", "--report", "report.json"])
        self.assertFalse((self.root / "absent.json").exists())

    def test_short_read_rejected_source_fault_injection(self):
        self.write("a.md", "# Original bytes\n")
        with mock.patch("builtins.open", return_value=io.BytesIO(b"#")):
            with self.assertRaisesRegex(MODULE.Reject, "incomplete read"):
                MODULE.read_text_file(self.root / "a.md")

    def test_permission_error_is_refused_source_fault_injection(self):
        self.write("a.md", "# A\n")
        with mock.patch("os.getcwd", return_value=str(self.root)), \
                mock.patch.object(MODULE, "read_text_file", side_effect=PermissionError("permission denied")):
            self.assertEqual(MODULE.run(["--input", "a.md", "--report", str("report.json")]), 0)
        rep = self.report()
        self.assertEqual(rep["status"], "refused")
        self.assertEqual(rep["unreadable_inputs"], [{"path": "a.md", "error": "permission denied"}])
        self.assertEqual(rep["counts"]["files_failed"], 1)

    def test_actual_permission_denial_when_enforced(self):
        self.write("a.md", "# A\n")
        path = self.root / "a.md"
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        if os.access(path, os.R_OK):
            self.skipTest("execution identity can read mode-000 files; deterministic injection tested separately")
        result = run_audit(self.root, "--input", "a.md", "--report", "report.json")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.report()["status"], "refused")

    def test_setext_and_reference_images_not_silently_clean(self):
        self.write("a.md", "Title\n=====\n\nSubtitle\n---\n\n![image][missing]\n![image][]\n")
        result = run_audit(self.root, "--input", "a.md", "--report", "report.json")
        self.assertEqual(result.returncode, 0, result.stdout)
        rep = self.report()
        self.assertEqual(rep["status"], "issues")
        self.assertEqual(rep["counts"]["unsupported_constructs"], 4)
        self.assertEqual(rep["counts"]["links_checked"], 0)

    def test_nested_parenthesis_destinations_and_titles(self):
        self.write("docs/a.md", '[one](path(with-parens).md) [two](../root.md "title")\n'
                   '[three](<path(with-parens).md>) [four](path(with-parens).md (title))\n')
        self.write("docs/path(with-parens).md", "# Target\n")
        self.write("root.md", "# Root\n")
        result = run_audit(self.root, "--input", "docs/a.md", "--report", "report.json")
        self.assertEqual(result.returncode, 0, result.stdout)
        rep = self.report()
        self.assertEqual(rep["status"], "ok", rep)
        self.assertEqual(rep["counts"]["links_checked"], 4)

    def test_unbalanced_destination_not_checked_as_truncated_path(self):
        self.write("a.md", "[x](nested(path.md)\n")
        run_audit(self.root, "--input", "a.md", "--report", "report.json")
        rep = self.report()
        self.assertEqual(rep["counts"]["unsupported_constructs"], 1)
        self.assertEqual(rep["counts"]["links_checked"], 0)
        self.assertEqual(self.rules(rep), ["unsupported-construct"])

    def test_fence_trailing_text_does_not_close(self):
        self.write("a.md", "```\n``` not closing\n[x](missing.md)\n```\n"
                   "~~~\n~~~ not closing\n[y](missing.md)\n~~~   \n"
                   "[real](a.md)\n")
        run_audit(self.root, "--input", "a.md", "--report", "report.json")
        rep = self.report()
        self.assertEqual(rep["status"], "ok", rep)
        self.assertEqual(rep["counts"]["links_checked"], 1)

    def test_autolinks_counted_once_and_code_ignored(self):
        self.write("a.md", "<https://example.invalid> <mailto:a@b.test> <a@b.test>\n"
                   "[inline](<https://example.invalid>) `<https://ignored.invalid>`\n"
                   "```\n<https://ignored.invalid>\n```\nhttps://bare.invalid\n")
        run_audit(self.root, "--input", "a.md", "--report", "report.json")
        rep = self.report()
        self.assertEqual(rep["status"], "ok", rep)
        self.assertEqual(rep["counts"]["external_skipped"], 4)

    def test_unmatched_bracket_runs_finish_without_quadratic_search(self):
        # Exercise both inline and reference recognition through the real CLI.
        # A generous timeout catches the former quadratic 100k-bracket scan;
        # 500k characters remain far below the normal 16 MiB file limit.
        self.write("a.md", "[" * 250000 + "\n" + "[x]" + "[" * 250000 + "\n")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--input", "a.md", "--report", "report.json"],
            cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rep = self.report()
        self.assertEqual(rep["status"], "ok", rep)
        self.assertEqual(rep["counts"]["links_checked"], 0)

    def test_nested_inline_label_explicitly_unsupported_not_partial(self):
        self.write("a.md", "[outer [inner]](missing.md) ![outer [alt]](missing.png)\n"
                   "[normal](a.md)\n")
        result = run_audit(self.root, "--input", "a.md", "--report", "report.json")
        self.assertEqual(result.returncode, 0, result.stdout)
        rep = self.report()
        self.assertEqual(rep["counts"]["unsupported_constructs"], 2)
        self.assertEqual(rep["counts"]["links_checked"], 1)
        self.assertEqual(self.rules(rep), ["unsupported-construct"])


if __name__ == "__main__":
    unittest.main()
