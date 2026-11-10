#!/usr/bin/env python3
"""Run opencode as a subagent for Claude Code, Codex, and other skill-aware agents.

Standard library only. Python 3.9+. Works on Linux, macOS, and Windows.

Subcommands:
  run     Run one task; print the agent's final answer plus a one-line footer.
  batch   Run several tasks in parallel from a JSON file; write each result to a dir.
  models  List available models (optional substring filter).
  doctor  Show the resolved binary, version, default model, and whether it is available.
  bin     Print the resolved opencode binary path.

Examples:
  oc.py run "Summarize README.md"
  oc.py run --agent explore "Where is JWT verified? Return file:line list."
  oc.py run --auto --worktree fix-x "Fix the failing test in foo_test.py"
  oc.py run --prompt-file task.md --out result.md
  oc.py batch tasks.json --out-dir results --jobs 4
  oc.py models deepseek

Configuration (environment):
  OPENCODE_BIN       Path to the opencode executable (skips auto-discovery).
  OC_DEFAULT_MODEL   Model used when -m is omitted. Set to "default" to use
                     opencode's own configured model. Built-in default:
                     opencode-go/deepseek-v4.1-flash
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

__version__ = "1.0.0"

BUILTIN_DEFAULT_MODEL = "opencode-go/deepseek-v4.1-flash"


def default_model() -> str:
    return os.environ.get("OC_DEFAULT_MODEL") or BUILTIN_DEFAULT_MODEL


def known_paths() -> list:
    home = Path.home()
    paths = [
        home / ".opencode" / "bin" / "opencode",
        home / ".opencode" / "bin" / "opencode.exe",
        home / ".local" / "bin" / "opencode",
        home / ".bun" / "bin" / "opencode",
        home / ".npm-global" / "bin" / "opencode",
    ]
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            paths.insert(0, Path(local) / "Programs" / "ai.opencode.desktop" / "resources" / "opencode-cli.exe")
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "npm" / "opencode.cmd")
    elif sys.platform == "darwin":
        paths += [
            Path("/opt/homebrew/bin/opencode"),
            Path("/usr/local/bin/opencode"),
            Path("/Applications/OpenCode.app/Contents/Resources/opencode-cli"),
            Path("/Applications/OpenCode.app/Contents/MacOS/opencode-cli"),
        ]
    else:
        paths += [
            Path("/usr/local/bin/opencode"),
            Path("/usr/bin/opencode"),
            Path("/opt/OpenCode/resources/opencode-cli"),
        ]
    return paths


def resolve_bin() -> list:
    """Return the command prefix used to invoke opencode."""
    env = os.environ.get("OPENCODE_BIN")
    if env:
        if Path(env).exists() or shutil.which(env):
            return [shutil.which(env) or env]
        sys.exit(f"oc: OPENCODE_BIN={env!r} does not exist.")
    for name in ("opencode", "opencode-cli"):
        found = shutil.which(name)
        if found:
            return [found]
    for p in known_paths():
        if p.is_file():
            return [str(p)]
    sys.exit(
        "oc: opencode binary not found. Install opencode (https://opencode.ai) "
        "or set OPENCODE_BIN to its executable."
    )


def build_cmd(t: dict) -> list:
    cmd = resolve_bin() + ["run", "--archived", "--format", "json"]
    model = t.get("model") or default_model()
    t["model"] = model
    if model != "default":
        cmd += ["-m", model]
    if t.get("agent"):
        cmd += ["--agent", t["agent"]]
    if t.get("auto"):
        cmd.append("--auto")
    wt = t.get("worktree")
    if wt:
        cmd.append("--worktree" if wt is True else f"--worktree={wt}")
    if t.get("session"):
        cmd += ["-s", t["session"]]
    if t.get("continue"):
        cmd.append("-c")
    if t.get("fork"):
        cmd.append("--fork")
    if t.get("thinking"):
        cmd.append("--thinking")
    if t.get("title"):
        cmd += ["--title", t["title"]]
    for f in t.get("files") or []:
        cmd += ["-f", str(Path(f).resolve())]
    prompt = t.get("prompt") or ""
    if t.get("prompt_file"):
        # Attach the file instead of putting it on argv (Windows argv limit ~32k chars).
        pf = Path(t["prompt_file"]).resolve()
        if not pf.is_file():
            raise ValueError(f"prompt_file not found: {pf}")
        cmd += ["-f", str(pf)]
        prompt = (prompt + "\n\n" if prompt else "") + (
            f"Your full task instructions are in the attached file {pf.name}. "
            "Read it and follow it exactly."
        )
    if not prompt.strip():
        raise ValueError("task needs a prompt or prompt_file")
    cmd.append(prompt)
    return cmd


def _new_result(t: dict, cwd: str) -> dict:
    return {
        "name": t.get("name"), "session": None, "text": "", "all_text": [],
        "tools": {}, "steps": 0,
        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0},
        "cost": 0.0, "errors": [], "exit": None, "timed_out": False, "stderr": "",
        "model": t.get("model"), "agent": t.get("agent") or "(default)", "cwd": cwd,
        "elapsed": 0.0,
    }


def run_task(t: dict, raw_stream=None) -> dict:
    """Run one opencode task and return a result dict."""
    cmd = build_cmd(t)
    cwd = t.get("cwd") or os.getcwd()
    timeout = t.get("timeout") or 1800
    started = time.time()
    res = _new_result(t, cwd)
    msg_text = {}
    last_text_msg = None

    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace",
    )
    err_chunks = []
    reader = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    reader.start()

    def _on_timeout():
        res["timed_out"] = True
        proc.kill()

    timer = threading.Timer(timeout, _on_timeout)
    timer.start()
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if raw_stream:
                raw_stream.write(line + "\n")
                raw_stream.flush()
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            res["session"] = res["session"] or ev.get("sessionID")
            etype = ev.get("type")
            part = ev.get("part") or {}
            if etype == "text":
                mid = part.get("messageID", "?")
                msg_text.setdefault(mid, []).append(part.get("text", ""))
                last_text_msg = mid
            elif etype == "tool_use":
                name = part.get("tool", "?")
                res["tools"][name] = res["tools"].get(name, 0) + 1
                st = part.get("state") or {}
                if st.get("status") == "error":
                    res["errors"].append(f"tool {name}: {str(st.get('error'))[:300]}")
            elif etype == "step_finish":
                res["steps"] += 1
                tk = part.get("tokens") or {}
                res["tokens"]["input"] += tk.get("input") or 0
                res["tokens"]["output"] += tk.get("output") or 0
                res["tokens"]["reasoning"] += tk.get("reasoning") or 0
                res["tokens"]["cache_read"] += (tk.get("cache") or {}).get("read") or 0
                try:
                    res["cost"] += float(part.get("cost") or 0)
                except (TypeError, ValueError):
                    pass
            elif etype == "error" or "error" in ev:
                res["errors"].append(str(ev.get("error") or part)[:500])
        proc.wait()
    finally:
        timer.cancel()
        reader.join(timeout=5)
    res["exit"] = proc.returncode
    res["stderr"] = "".join(err_chunks)[-2000:]
    res["elapsed"] = round(time.time() - started, 1)
    res["all_text"] = ["".join(v) for v in msg_text.values()]
    res["text"] = "".join(msg_text.get(last_text_msg, [])) if last_text_msg else ""
    return res


def ok(r: dict) -> bool:
    return r["exit"] == 0 and not r["timed_out"]


def footer(r: dict) -> str:
    tools = ",".join(f"{k}x{v}" for k, v in sorted(r["tools"].items())) or "none"
    tk = r["tokens"]
    status = "TIMEOUT" if r["timed_out"] else f"exit={r['exit']}"
    return (
        f"[opencode] {status} session={r['session']} model={r['model']} agent={r['agent']} "
        f"steps={r['steps']} tools={tools} tokens(in={tk['input']} out={tk['output']} "
        f"reason={tk['reasoning']} cache_read={tk['cache_read']}) cost=${r['cost']:.4f} "
        f"elapsed={r['elapsed']}s"
    )


def render(r: dict, all_text: bool = False) -> str:
    body = "\n\n".join(t for t in r["all_text"] if t.strip()) if all_text else r["text"]
    out = [body.rstrip() or "(no text output)"]
    if r["errors"]:
        out.append("[opencode errors]\n" + "\n".join(r["errors"]))
    if (not ok(r) or not r["text"]) and r["stderr"].strip():
        out.append("[opencode stderr tail]\n" + r["stderr"].strip()[-1200:])
    out.append("---\n" + footer(r))
    return "\n\n".join(out) + "\n"


def task_from_args(a) -> dict:
    prompt = " ".join(a.prompt) if a.prompt else ""
    if prompt == "-":
        prompt = sys.stdin.read()
    return {
        "prompt": prompt, "prompt_file": a.prompt_file, "model": a.model, "agent": a.agent,
        "auto": a.auto, "worktree": (True if a.worktree == "" else a.worktree),
        "session": a.session, "continue": a.cont, "fork": a.fork, "thinking": a.thinking,
        "title": a.title, "files": a.file, "cwd": a.cwd, "timeout": a.timeout,
    }


def cmd_run(a) -> int:
    t = task_from_args(a)
    raw = open(a.raw, "w", encoding="utf-8") if a.raw else None
    try:
        r = run_task(t, raw_stream=raw)
    except ValueError as e:
        print(f"oc: {e}", file=sys.stderr)
        return 2
    finally:
        if raw:
            raw.close()
    text = render(r, a.all_text)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out}\n---\n{footer(r)}")
    else:
        sys.stdout.write(text)
    return 0 if ok(r) else 1


def load_batch(path: str) -> list:
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    defaults = spec.get("defaults", {}) if isinstance(spec, dict) else {}
    tasks = spec["tasks"] if isinstance(spec, dict) else spec
    base = Path(path).resolve().parent
    for i, t in enumerate(tasks):
        for k, v in defaults.items():
            t.setdefault(k, v)
        t.setdefault("name", f"task{i + 1}")
        t.setdefault("title", f"oc-batch:{t['name']}")
        # Relative prompt_file / files resolve against the batch file's directory.
        if t.get("prompt_file") and not Path(t["prompt_file"]).is_absolute():
            t["prompt_file"] = str(base / t["prompt_file"])
        if t.get("files"):
            t["files"] = [f if Path(f).is_absolute() else str(base / f) for f in t["files"]]
    names = [t["name"] for t in tasks]
    if len(set(names)) != len(names):
        raise ValueError("batch task names must be unique")
    return tasks


def cmd_batch(a) -> int:
    try:
        tasks = load_batch(a.file)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        print(f"oc: bad batch file: {e}", file=sys.stderr)
        return 2
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        futs = {ex.submit(run_task, t): t for t in tasks}
        for f in cf.as_completed(futs):
            t = futs[f]
            try:
                r = f.result()
            except Exception as e:  # noqa: BLE001
                print(f"[{t['name']}] FAILED to start: {e}", flush=True)
                continue
            path = out_dir / f"{t['name']}.md"
            path.write_text(render(r, a.all_text), encoding="utf-8")
            results[t["name"]] = r
            print(f"[{t['name']}] -> {path}\n  {footer(r)}", flush=True)
    keys = ("session", "exit", "timed_out", "elapsed", "cost", "tokens", "model", "agent")
    summary = {n: {k: r[k] for k in keys} for n, r in results.items()}
    (out_dir / "_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    all_ok = len(results) == len(tasks) and all(ok(r) for r in results.values())
    print(f"batch done: {len(results)}/{len(tasks)} completed, ok={all_ok}, "
          f"summary={out_dir / '_summary.json'}")
    return 0 if all_ok else 1


def _models() -> tuple:
    out = subprocess.run(resolve_bin() + ["models"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return out.returncode, [l.strip() for l in out.stdout.splitlines() if l.strip()]


def cmd_models(a) -> int:
    rc, lines = _models()
    if a.filter:
        lines = [l for l in lines if a.filter.lower() in l.lower()]
    print("\n".join(lines))
    return rc


def cmd_doctor(a) -> int:
    b = resolve_bin()
    ver = subprocess.run(b + ["--version"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    model = default_model()
    print(f"oc.py      {__version__}")
    print(f"python     {sys.version.split()[0]} ({sys.platform})")
    print(f"binary     {b[0]}")
    print(f"version    {(ver.stdout or ver.stderr).strip() or 'unknown'}")
    print(f"default    {model}" + (" (env OC_DEFAULT_MODEL)" if os.environ.get("OC_DEFAULT_MODEL") else ""))
    if model == "default":
        print("model      using opencode's configured default")
        return 0
    rc, lines = _models()
    available = model in lines
    print(f"available  {'yes' if available else 'NO - run `oc.py models` and set OC_DEFAULT_MODEL or pass -m'}")
    return 0 if available else 1


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(prog="oc.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    r = sub.add_parser("run", help="run one task")
    r.add_argument("prompt", nargs="*", help="task text; '-' reads stdin")
    r.add_argument("--prompt-file", help="long instructions file (attached; avoids argv limits)")
    r.add_argument("-m", "--model", help="provider/model[#variant], or 'default' for opencode's own")
    r.add_argument("--agent", help="opencode agent, e.g. build, plan, general, explore")
    r.add_argument("--auto", action="store_true", help="auto-approve permissions (edits, commands)")
    r.add_argument("--worktree", nargs="?", const="", default=None, help="run in a new git worktree [name]")
    r.add_argument("-s", "--session", help="continue or create this session id")
    r.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue last session")
    r.add_argument("--fork", action="store_true", help="fork the session before continuing")
    r.add_argument("--thinking", action="store_true")
    r.add_argument("--title")
    r.add_argument("-f", "--file", action="append", help="attach a file (repeatable)")
    r.add_argument("--cwd", help="working directory (default: current)")
    r.add_argument("--timeout", type=int, default=1800, help="seconds (default 1800)")
    r.add_argument("--out", help="write the result to this file instead of stdout")
    r.add_argument("--raw", help="also save raw JSON events to this file")
    r.add_argument("--all-text", action="store_true", help="include text from every step")
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("batch", help="run tasks from a JSON file in parallel")
    b.add_argument("file", help='JSON: [{name,prompt,...}] or {"defaults":{...},"tasks":[...]}')
    b.add_argument("--out-dir", default="oc-results")
    b.add_argument("--jobs", type=int, default=4)
    b.add_argument("--all-text", action="store_true")
    b.set_defaults(fn=cmd_batch)

    m = sub.add_parser("models", help="list models")
    m.add_argument("filter", nargs="?")
    m.set_defaults(fn=cmd_models)

    d = sub.add_parser("doctor", help="check binary, version, and default model")
    d.set_defaults(fn=cmd_doctor)

    bn = sub.add_parser("bin", help="print the resolved binary path")
    bn.set_defaults(fn=lambda a: (print(resolve_bin()[0]), 0)[1])

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
