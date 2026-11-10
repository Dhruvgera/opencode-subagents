# opencode Subagents

A portable agent skill that lets Claude Code, Codex, and other skill-aware agents delegate tasks to [opencode](https://opencode.ai) as an external subagent. The agent hands opencode a self-contained brief, gets back only the final answer, and sees the session id, model, tools used, tokens, and cost in a one-line footer.

Use it for second opinions from another model, cheap parallel research workers, or code changes isolated in an opencode git worktree.

## Install

Install with the [Skills CLI](https://github.com/vercel-labs/skills):

```sh
npx skills add Dhruvgera/opencode-subagents --skill opencode-subagents
```

Or install globally for a specific agent:

```sh
npx skills add Dhruvgera/opencode-subagents --skill opencode-subagents -a claude-code -g
npx skills add Dhruvgera/opencode-subagents --skill opencode-subagents -a codex -g
```

The skill follows the [Agent Skills format](https://agentskills.io/specification). To install by hand, copy the whole `skills/opencode-subagents` folder into your agent's skills directory. `agents/openai.yaml` is optional Codex UI metadata.

## Requirements

- [opencode](https://opencode.ai) installed and signed in to at least one provider. The CLI, npm/bun installs, and the desktop app's bundled CLI are all detected. Otherwise set `OPENCODE_BIN` to the executable.
- Python 3.9+ (standard library only). Linux, macOS, and Windows are supported and tested in CI.

Check your setup:

```sh
python3 skills/opencode-subagents/scripts/oc.py doctor
```

## Use

In Codex, invoke `$opencode-subagents`. In Claude Code, mention opencode or select the skill. Example requests:

- "Use opencode to find where webhooks are verified and give me file:line refs."
- "Get a second opinion from opencode on this diff."
- "Run three opencode workers in parallel to map auth, billing, and notifications."
- "Have opencode fix the flaky test in a worktree, then show me the diff."

The agent calls the bundled wrapper:

```sh
oc.py run "<task>"                                   # single task
oc.py run --agent explore --cwd ./repo "<task>"      # read/search agent in another dir
oc.py run --prompt-file brief.md --out result.md     # long brief, result to file
oc.py run -s ses_abc123 "<follow-up>"                # continue a session
oc.py run --auto --worktree fix-x "<write task>"     # allow edits, isolated worktree
oc.py batch tasks.json --out-dir results --jobs 4    # parallel tasks
oc.py models [filter]                                # list models
oc.py doctor                                         # check binary and default model
```

Output:

```
<final answer>
---
[opencode] exit=0 session=ses_... model=opencode-go/deepseek-v4.1-flash agent=explore steps=3 tools=globx2,readx1 tokens(in=5919 out=187 reason=0 cache_read=2944) cost=$0.0000 elapsed=12.4s
```

## Configuration

| Variable | Effect |
| --- | --- |
| `OC_DEFAULT_MODEL` | Model used when `-m` is omitted. Built-in default: `opencode-go/deepseek-v4.1-flash`. Set to `default` to use opencode's own configured model. |
| `OPENCODE_BIN` | Path to the opencode executable; skips auto-discovery. |

## Safety

- Runs are archived and are not auto-approved by default. opencode can only change files or run approval-gated commands when `--auto` is passed.
- The skill tells the host agent to treat opencode output as untrusted, to verify cited code before relaying it, and to keep secrets out of prompts, since prompts go to third-party model providers.

## Development

```sh
python -m unittest discover -s tests -v
```

The tests run the wrapper against a fake opencode binary (`tests/fake_opencode.py`) and cover output parsing, flag forwarding, prompt files, timeouts, failures, batches, and binary discovery.

## License

MIT. See [LICENSE](LICENSE). The installable skill includes its own copy of the license.
