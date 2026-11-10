"""Stand-in for the opencode CLI used by the test suite.

Behavior is selected with FAKE_MODE: normal (default), fail, hang, garbage.
If FAKE_ARGV_OUT is set, argv is written there as JSON.
"""
import json
import os
import sys
import time

args = sys.argv[1:]
if os.environ.get("FAKE_ARGV_OUT"):
    with open(os.environ["FAKE_ARGV_OUT"], "w", encoding="utf-8") as f:
        json.dump({"argv": args, "cwd": os.getcwd()}, f)

if args[:1] == ["--version"]:
    print("opencode v0.0.0-fake")
    sys.exit(0)
if args[:1] == ["models"]:
    print("opencode-go/deepseek-v4.1-flash\nopencode-go/other-model\nanthropic/some-model")
    sys.exit(0)

mode = os.environ.get("FAKE_MODE", "normal")
sid = "ses_fake123"


def emit(etype, part):
    print(json.dumps({"type": etype, "timestamp": 1, "sessionID": sid, "part": part}), flush=True)


if mode == "fail":
    sys.stderr.write("Error: provider rejected the request\n")
    sys.exit(3)
if mode == "hang":
    emit("step_start", {"type": "step-start", "messageID": "m1"})
    time.sleep(60)
    sys.exit(0)
if mode == "garbage":
    print("not json at all")
    print("[1, 2, 3]")

prompt = args[-1] if args else ""
emit("step_start", {"type": "step-start", "messageID": "m1"})
emit("text", {"type": "text", "messageID": "m1", "text": "Let me look."})
emit("tool_use", {"type": "tool", "tool": "read", "messageID": "m1",
                  "state": {"status": "completed", "input": {"path": "x"}}})
emit("tool_use", {"type": "tool", "tool": "grep", "messageID": "m1",
                  "state": {"status": "error", "error": "no such dir"}})
emit("step_finish", {"type": "step-finish", "messageID": "m1", "reason": "tool-calls", "cost": 0.001,
                     "tokens": {"input": 100, "output": 10, "reasoning": 5, "cache": {"read": 50, "write": 0}}})
emit("step_start", {"type": "step-start", "messageID": "m2"})
emit("text", {"type": "text", "messageID": "m2", "text": "FINAL: "})
emit("text", {"type": "text", "messageID": "m2", "text": "echo=" + prompt[:40]})
emit("step_finish", {"type": "step-finish", "messageID": "m2", "reason": "stop", "cost": "0.002",
                     "tokens": {"input": 20, "output": 4, "reasoning": 0, "cache": {"read": 7, "write": 0}}})
