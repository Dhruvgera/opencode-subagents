"""Behavior tests for scripts/oc.py using a fake opencode binary.

Run: python -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "opencode-subagents" / "scripts"))
import oc  # noqa: E402

FAKE = [sys.executable, str(Path(__file__).with_name("fake_opencode.py"))]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.argv_out = self.dir / "argv.json"
        patches = [
            mock.patch.object(oc, "resolve_bin", return_value=list(FAKE)),
            mock.patch.dict(os.environ, {"FAKE_ARGV_OUT": str(self.argv_out)}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("OC_DEFAULT_MODEL", None)
        os.environ.pop("FAKE_MODE", None)

    def tearDown(self):
        self.tmp.cleanup()

    def argv(self):
        return json.loads(self.argv_out.read_text(encoding="utf-8"))["argv"]

    def call(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = oc.main(list(args))
        return rc, buf.getvalue()


class RunTests(Base):
    def test_returns_only_final_message_and_footer(self):
        rc, out = self.call("run", "hello world")
        self.assertEqual(rc, 0)
        body, foot = out.split("---\n")
        self.assertIn("FINAL: echo=hello world", body)
        self.assertNotIn("Let me look.", body)
        self.assertIn("session=ses_fake123", foot)
        self.assertIn("steps=2", foot)
        self.assertIn("tools=grepx1,readx1", foot)
        self.assertIn("tokens(in=120 out=14 reason=5 cache_read=57)", foot)
        self.assertIn("cost=$0.0030", foot)

    def test_tool_error_is_surfaced(self):
        _, out = self.call("run", "x")
        self.assertIn("tool grep: no such dir", out)

    def test_all_text_includes_intermediate_steps(self):
        _, out = self.call("run", "--all-text", "x")
        self.assertIn("Let me look.", out)

    def test_default_flags_and_model(self):
        self.call("run", "do it")
        a = self.argv()
        self.assertEqual(a[:4], ["run", "--archived", "--format", "json"])
        self.assertEqual(a[a.index("-m") + 1], "opencode-go/deepseek-v4.1-flash")
        self.assertNotIn("--auto", a)
        self.assertEqual(a[-1], "do it")

    def test_env_model_override_and_default_keyword(self):
        with mock.patch.dict(os.environ, {"OC_DEFAULT_MODEL": "x/y"}):
            self.call("run", "p")
        self.assertEqual(self.argv()[self.argv().index("-m") + 1], "x/y")
        with mock.patch.dict(os.environ, {"OC_DEFAULT_MODEL": "default"}):
            _, out = self.call("run", "p")
        self.assertNotIn("-m", self.argv())
        self.assertIn("model=default", out)

    def test_explicit_flags_forwarded(self):
        self.call("run", "-m", "a/b", "--agent", "explore", "--auto", "--worktree", "wt1",
                  "-s", "ses_1", "--title", "T", "-f", str(self.argv_out.parent / "f.txt"), "p")
        a = self.argv()
        for flag in (["-m", "a/b"], ["--agent", "explore"], ["-s", "ses_1"], ["--title", "T"]):
            i = a.index(flag[0])
            self.assertEqual(a[i:i + 2], flag)
        self.assertIn("--auto", a)
        self.assertIn("--worktree=wt1", a)
        self.assertIn("-f", a)

    def test_bare_worktree_flag(self):
        self.call("run", "--worktree", "--", "p")
        self.assertIn("--worktree", self.argv())

    def test_prompt_file_is_attached_not_inlined(self):
        brief = self.dir / "brief.md"
        brief.write_text("SECRET-LONG-BRIEF " * 500, encoding="utf-8")
        rc, _ = self.call("run", "--prompt-file", str(brief))
        self.assertEqual(rc, 0)
        a = self.argv()
        self.assertEqual(Path(a[a.index("-f") + 1]), brief.resolve())
        self.assertNotIn("SECRET-LONG-BRIEF", a[-1])
        self.assertIn("brief.md", a[-1])

    def test_missing_prompt_is_usage_error(self):
        rc, _ = self.call("run")
        self.assertEqual(rc, 2)

    def test_missing_prompt_file_is_usage_error(self):
        rc, _ = self.call("run", "--prompt-file", str(self.dir / "nope.md"))
        self.assertEqual(rc, 2)

    def test_cwd_is_honored(self):
        self.call("run", "--cwd", str(self.dir), "p")
        cwd = json.loads(self.argv_out.read_text(encoding="utf-8"))["cwd"]
        self.assertEqual(Path(cwd).resolve(), self.dir.resolve())

    def test_failure_reports_exit_and_stderr(self):
        with mock.patch.dict(os.environ, {"FAKE_MODE": "fail"}):
            rc, out = self.call("run", "p")
        self.assertEqual(rc, 1)
        self.assertIn("exit=3", out)
        self.assertIn("provider rejected the request", out)
        self.assertIn("(no text output)", out)

    def test_timeout_kills_process(self):
        started = time.time()
        with mock.patch.dict(os.environ, {"FAKE_MODE": "hang"}):
            rc, out = self.call("run", "--timeout", "2", "p")
        self.assertEqual(rc, 1)
        self.assertIn("TIMEOUT", out)
        # The fake sleeps 60s; returning quickly proves the process was killed.
        self.assertLess(time.time() - started, 20)

    def test_non_json_lines_are_ignored(self):
        with mock.patch.dict(os.environ, {"FAKE_MODE": "garbage"}):
            rc, out = self.call("run", "p")
        self.assertEqual(rc, 0)
        self.assertIn("FINAL: echo=p", out)

    def test_out_and_raw_files(self):
        out_f, raw_f = self.dir / "r.md", self.dir / "ev.jsonl"
        rc, out = self.call("run", "--out", str(out_f), "--raw", str(raw_f), "p")
        self.assertEqual(rc, 0)
        self.assertIn("FINAL: echo=p", out_f.read_text(encoding="utf-8"))
        self.assertNotIn("FINAL", out)
        lines = raw_f.read_text(encoding="utf-8").splitlines()
        self.assertTrue(all(json.loads(l)["sessionID"] == "ses_fake123" for l in lines))


class BatchTests(Base):
    def write_batch(self, spec):
        f = self.dir / "tasks.json"
        f.write_text(json.dumps(spec), encoding="utf-8")
        return f

    def test_parallel_batch_writes_results_and_summary(self):
        (self.dir / "b.md").write_text("brief", encoding="utf-8")
        f = self.write_batch({"defaults": {"agent": "explore"},
                              "tasks": [{"name": "one", "prompt": "first"},
                                        {"name": "two", "prompt_file": "b.md"}]})
        out_dir = self.dir / "res"
        rc, out = self.call("batch", str(f), "--out-dir", str(out_dir), "--jobs", "2")
        self.assertEqual(rc, 0)
        self.assertIn("FINAL: echo=first", (out_dir / "one.md").read_text(encoding="utf-8"))
        self.assertTrue((out_dir / "two.md").exists())
        summary = json.loads((out_dir / "_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(set(summary), {"one", "two"})
        self.assertEqual(summary["one"]["agent"], "explore")
        self.assertIn("2/2 completed, ok=True", out)

    def test_batch_failure_sets_exit_code(self):
        f = self.write_batch([{"name": "a", "prompt": "p"}])
        with mock.patch.dict(os.environ, {"FAKE_MODE": "fail"}):
            rc, out = self.call("batch", str(f), "--out-dir", str(self.dir / "r"))
        self.assertEqual(rc, 1)
        self.assertIn("ok=False", out)

    def test_duplicate_names_rejected(self):
        f = self.write_batch([{"name": "a", "prompt": "p"}, {"name": "a", "prompt": "q"}])
        rc, _ = self.call("batch", str(f), "--out-dir", str(self.dir / "r"))
        self.assertEqual(rc, 2)


class ToolingTests(Base):
    def test_models_filter(self):
        rc, out = self.call("models", "deepseek")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "opencode-go/deepseek-v4.1-flash")

    def test_doctor_reports_available_default(self):
        rc, out = self.call("doctor")
        self.assertEqual(rc, 0)
        self.assertIn("opencode v0.0.0-fake", out)
        self.assertIn("available  yes", out)

    def test_doctor_flags_missing_model(self):
        with mock.patch.dict(os.environ, {"OC_DEFAULT_MODEL": "nope/missing"}):
            rc, out = self.call("doctor")
        self.assertEqual(rc, 1)
        self.assertIn("available  NO", out)


class ResolveBinTests(unittest.TestCase):
    def test_opencode_bin_env_wins(self):
        with mock.patch.dict(os.environ, {"OPENCODE_BIN": sys.executable}):
            self.assertEqual(oc.resolve_bin(), [sys.executable])

    def test_bad_opencode_bin_exits(self):
        with mock.patch.dict(os.environ, {"OPENCODE_BIN": "/definitely/not/here/opencode"}):
            with self.assertRaises(SystemExit):
                oc.resolve_bin()

    def test_not_found_exits_with_hint(self):
        env = {k: v for k, v in os.environ.items() if k != "OPENCODE_BIN"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(oc.shutil, "which", return_value=None), \
                mock.patch.object(oc, "known_paths", return_value=[]):
            with self.assertRaises(SystemExit) as cm:
                oc.resolve_bin()
        self.assertIn("OPENCODE_BIN", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
