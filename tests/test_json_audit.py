import decimal
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "plugins/community.json-audit/json_audit.py"


def run_audit(root, *args, **kwargs):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=root, capture_output=True, text=True, timeout=60)


class JsonAuditTests(unittest.TestCase):
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
        return rel

    def report(self, rel="report.json"):
        return json.loads((self.root / rel).read_text("utf-8"))

    def test_ok_json(self):
        self.write("in.json", '{"a": 1, "b": [true, null, 1.5e3, "x"]}')
        r = run_audit(self.root, "--input", "in.json", "--mode", "json",
                      "--report", "report.json")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        rep = self.report()
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["input"]["bytes"], os.path.getsize(self.root / "in.json"))
        self.assertEqual(rep["structure"]["integers"], 1)
        self.assertEqual(rep["structure"]["decimals"], 1)
        self.assertEqual(rep["structure"]["arrays"], 1)

    def test_syntax_error_and_scalar_root(self):
        self.write("bad.json", '{"a": 1,}')
        r = run_audit(self.root, "--input", "bad.json", "--mode", "json",
                      "--report", "r.json")
        self.assertEqual(r.returncode, 0)
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "issues")
        self.assertEqual(rep["findings"][0]["rule"], "syntax-error")
        self.assertIn("line 1", rep["findings"][0]["message"])

        self.write("scalar.json", '"just a string"')
        r = run_audit(self.root, "--input", "scalar.json", "--mode", "json",
                      "--report", "r2.json")
        rep = self.report("r2.json")
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["structure"]["strings"], 1)

    def test_duplicate_keys_at_multiple_depths(self):
        self.write("dup.json",
                   '{"a": 1, "a": 2, "o": {"a": 1, "a": 2}, "l": [{"x":1,"x":2}]}')
        r = run_audit(self.root, "--input", "dup.json", "--mode", "json",
                      "--report", "r.json")
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "issues")
        rules = [f["rule"] for f in rep["findings"]]
        self.assertEqual(rules, ["duplicate-key"] * 3)
        paths = {f["path"] for f in rep["findings"]}
        self.assertIn('$."a"', paths)
        self.assertIn('$."o"."a"', paths)
        self.assertIn('$."l"[0]."x"', paths)
        self.assertEqual(rep["structure"]["duplicate_keys"], 3)
        self.assertEqual(rep["structure"]["object_keys"], 8)

    def test_escaped_and_unicode_keys(self):
        self.write("u.json", '{"k\\"ey": 1, "k\\"ey": 2, "键": "值"}')
        r = run_audit(self.root, "--input", "u.json", "--mode", "json",
                      "--report", "r.json")
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "issues")
        self.assertIn('k\\"ey', rep["findings"][0]["path"])

    def test_reject_nan_and_infinity(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                self.write("n.json", f'[ {literal} ]')
                r = run_audit(self.root, "--input", "n.json", "--mode", "json",
                              "--report", "rn.json")
                self.assertEqual(r.returncode, 0)
                rep = self.report("rn.json")
                self.assertEqual(rep["status"], "issues")
                os.unlink(self.root / "rn.json")

    def test_exact_numbers_not_lossy(self):
        big = 10 ** 40 + 7
        self.write("big.json", f'{{"big": {big}, "dec": 0.300000000000000000000001}}')
        r = run_audit(self.root, "--input", "big.json", "--mode", "json",
                      "--report", "r.json")
        self.assertEqual(r.returncode, 0)
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["structure"]["integers"], 1)
        self.assertEqual(rep["structure"]["decimals"], 1)
        # no float conversion artifacts in any message
        self.assertNotIn("e+", json.dumps(rep))

    def test_long_integer_tokens_are_exact_without_global_limit_changes(self):
        spec = importlib.util.spec_from_file_location("json_audit_under_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        before = sys.get_int_max_str_digits() if hasattr(sys, "get_int_max_str_digits") else None
        with decimal.localcontext() as context:
            context.prec = 3
            auditor = module.Auditor([])
            digits = "1234567890" * 500
            for token in (digits, "-" + digits):
                value = auditor.parse(token)
                # Check actual parsed value, not only a count-only report.
                self.assertIsInstance(value, decimal.Decimal)
                self.assertEqual(str(value), token)
                auditor.walk(value)
            for token in ("1.0", "1e0", "0.300000000000000000000001"):
                value = auditor.parse(token)
                self.assertEqual(value, decimal.Decimal(token))
                auditor.walk(value)
            auditor.walk(auditor.parse("true"))
            self.assertEqual(auditor.counts["integers"], 2)
            self.assertEqual(auditor.counts["decimals"], 3)
            self.assertEqual(auditor.counts["booleans"], 1)
        if before is not None:
            self.assertEqual(sys.get_int_max_str_digits(), before)

    def test_long_integer_cli_json_and_jsonl_preserve_input(self):
        digits = "9" * 5000
        for mode in ("json", "jsonl"):
            with self.subTest(mode=mode):
                content = ("[" + digits + ",-" + digits + ",1.0,1e0,true,null]\n").encode()
                input_name, report_name = "long." + mode, "long-" + mode + "-report.json"
                self.write(input_name, content)
                args = ("--input", input_name, "--mode", mode, "--report", report_name)
                result = run_audit(self.root, *args)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = self.report(report_name)
                self.assertEqual(report["status"], "ok")
                self.assertEqual(report["findings"], [])
                self.assertEqual(report["structure"]["integers"], 2)
                self.assertEqual(report["structure"]["decimals"], 2)
                self.assertEqual(report["structure"]["booleans"], 1)
                self.assertEqual((self.root / input_name).read_bytes(), content)
                previous_report = (self.root / report_name).read_bytes()
                self.assertNotIn(digits.encode(), previous_report)
                self.assertEqual(run_audit(self.root, *args).returncode, 2)
                self.assertEqual((self.root / report_name).read_bytes(), previous_report)
                self.assertEqual((self.root / input_name).read_bytes(), content)

    def test_jsonl_duplicate_physical_lines_and_nested_paths(self):
        content = ('\n{"a":1,"\\u0061":2}\n{bad}\n \n'
                   '{"nested":[{"a":3,"a":4}]}\n{"a":5,"a":6}\n')
        self.write("locations.jsonl", content)
        result = run_audit(self.root, "--input", "locations.jsonl", "--mode", "jsonl",
                           "--report", "locations-report.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = self.report("locations-report.json")
        self.assertEqual(report["status"], "issues")
        self.assertEqual([(f["rule"], f["path"], f.get("line")) for f in report["findings"]], [
            ("duplicate-key", '$."a"', 2),
            ("record-syntax-error", '$', 3),
            ("duplicate-key", '$."nested"[0]."a"', 5),
            ("duplicate-key", '$."a"', 6),
        ])
        self.assertEqual(report["jsonl"], {"physical_lines": 6, "blank_lines": 2,
                                          "records": 4, "record_errors": 1})
        self.assertEqual((self.root / "locations.jsonl").read_text(), content)

    def test_jsonl_records(self):
        self.write("d.jsonl",
                   '{"a":1}\n\n  \n{"b":2}\n{bad}\n{"c":3,"c":4}\n')
        r = run_audit(self.root, "--input", "d.jsonl", "--mode", "jsonl",
                      "--report", "r.json")
        self.assertEqual(r.returncode, 0)
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "issues")
        self.assertEqual(rep["jsonl"]["physical_lines"], 6)
        self.assertEqual(rep["jsonl"]["blank_lines"], 2)
        self.assertEqual(rep["jsonl"]["records"], 4)
        self.assertEqual(rep["jsonl"]["record_errors"], 1)
        rules = [f["rule"] for f in rep["findings"]]
        self.assertIn("record-syntax-error", rules)
        self.assertIn("duplicate-key", rules)
        line_finding = [f for f in rep["findings"]
                        if f["rule"] == "record-syntax-error"][0]
        self.assertEqual(line_finding["line"], 5)

    def test_jsonl_perfect_file(self):
        self.write("ok.jsonl", '{"a":1}\n{"b":2}\n')
        r = run_audit(self.root, "--input", "ok.jsonl", "--mode", "jsonl",
                      "--report", "r.json")
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "ok")
        self.assertEqual(rep["jsonl"]["records"], 2)

    def test_invalid_utf8(self):
        self.write("bad.bin", b'{"a": \xff\xfe}')
        r = run_audit(self.root, "--input", "bad.bin", "--mode", "json",
                      "--report", "r.json")
        self.assertEqual(r.returncode, 0)
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "issues")
        self.assertEqual(rep["findings"][0]["rule"], "invalid-utf8")

    def test_missing_input_and_report_conflicts(self):
        r = run_audit(self.root, "--input", "none.json", "--mode", "json",
                      "--report", "r.json")
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.root / "r.json").exists())

        self.write("in.json", "{}")
        self.write("exists.json", "x")
        r = run_audit(self.root, "--input", "in.json", "--mode", "json",
                      "--report", "exists.json")
        self.assertEqual(r.returncode, 2)
        self.assertEqual((self.root / "exists.json").read_text(), "x")

        r = run_audit(self.root, "--input", "in.json", "--mode", "json",
                      "--report", "in.json")
        self.assertEqual(r.returncode, 2)

        r = run_audit(self.root, "--input", "../out.json", "--mode", "json",
                      "--report", "r.json")
        self.assertEqual(r.returncode, 2)
        r = run_audit(self.root, "--input", "in.json", "--mode", "json",
                      "--report", "/tmp/abs.json")
        self.assertEqual(r.returncode, 2)

    def test_symlink_rejected(self):
        self.write("real.json", "{}")
        (self.root / "link.json").symlink_to(self.root / "real.json")
        r = run_audit(self.root, "--input", "link.json", "--mode", "json",
                      "--report", "r.json")
        self.assertEqual(r.returncode, 2)

    def test_directory_input_rejected(self):
        (self.root / "dir").mkdir()
        r = run_audit(self.root, "--input", "dir", "--mode", "json",
                      "--report", "r.json")
        self.assertEqual(r.returncode, 2)

    def test_depth_refusal(self):
        deep = "[" * 300 + "1" + "]" * 300
        self.write("deep.json", deep)
        r = run_audit(self.root, "--input", "deep.json", "--mode", "json",
                      "--report", "r.json")
        self.assertEqual(r.returncode, 0)
        rep = self.report("r.json")
        self.assertEqual(rep["status"], "refused")
        self.assertIn("depth", rep["refusal"])

    def test_input_not_modified(self):
        content = '{"a": 1, "a": 2}'
        self.write("in.json", content)
        before = (self.root / "in.json").read_bytes()
        run_audit(self.root, "--input", "in.json", "--mode", "json",
                  "--report", "r.json")
        self.assertEqual((self.root / "in.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
