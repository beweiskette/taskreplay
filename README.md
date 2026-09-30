# taskreplay

[Deutsche Version](README.de.md)

Benchmark coding agents on your own past tasks instead of public benchmarks.

SWE-bench, Terminal-Bench and similar leaderboards measure how agents do on other people's repositories. They do not tell you which agent handles the bug fixes in *your* code base, how long it takes, or what a solved task costs. taskreplay answers that from your git history: it takes finished tasks (the commit before, a task description, and a test command that failed before and passes after), replays each task with several agents in separate git worktrees, and records for every run:

- whether the tests pass
- wall time
- tokens in and out
- money cost, when the agent reports it or you configure prices
- number of turns
- diff size (files, lines added and removed)

The report shows, per agent, the pass rate, median time, median tokens and cost per solved task, broken down by task tags such as `bugfix` or `feature`. Runners exist for Claude Code, Codex CLI, any OpenAI-compatible endpoint (a small built-in agent loop, so cheap hosted models and local LM Studio or Ollama models can take part) and any other agent that you can start from a shell command.

**Real runs cost money or subscription quota.** Each run of the `claude`, `codex` or `openai-compatible` runner is a full agent session. Three tasks times three agents times two attempts are eighteen sessions. Use `--dry-run` first, and `--max-cost` and `--max-tasks` to cap a run.

## Install

Requires Python 3.11 or newer and git.

```
git clone https://github.com/beweiskette/taskreplay.git
cd taskreplay
python -m venv .venv
.venv/bin/python -m pip install -e .        # Windows: .venv\Scripts\python -m pip install -e .
```

The only runtime dependency is PyYAML. The agent CLIs you want to compare (`claude`, `codex`, ...) must be installed and logged in separately.

## Quick start without cost

`examples/demo.py` builds a tiny synthetic repository with one bug fix in its history and a task file with two scripted "agents": one writes the known fix, the other does nothing. No API is called. The demo's test command uses pytest, so install it into the same environment (`pip install pytest`).

```
python examples/demo.py demo
taskreplay validate demo/tasks.yaml
taskreplay run demo/tasks.yaml --runners scripted-fixer,do-nothing --attempts 2 --out demo/results
taskreplay report demo/results
```

Output of the last two commands (paths shortened):

```
running 4 run(s). Agent CLIs and API runners use real quota or money. No --max-cost set.
[1/4] slugify-punctuation with scripted-fixer (attempt 1)
    PASS  status=ok  0.1s  tokens=4200/350  $0.0179
[2/4] slugify-punctuation with do-nothing (attempt 1)
    FAIL  status=ok  0.1s  tokens n/a  cost n/a
...

runner          runs  pass rate   median time  median tokens  median turns  median diff  total cost  cost/solved  errors  timeouts
--------------  ----  ----------  -----------  -------------  ------------  -----------  ----------  -----------  ------  --------
scripted-fixer  2     100% (2/2)  0.1s         4.5k           3             6            $0.0357     $0.0179      0       0
do-nothing      2     0% (0/2)    0.1s         -              -             0            -           -            0       0

Passed runs by tag:
tag     scripted-fixer  do-nothing
------  --------------  ----------
bugfix  2/2             0/2

Best runner per tag (pass rate, then cost per solved task, then time):
  bugfix: scripted-fixer

HTML report: demo/results/report.html
```

The HTML report is one self-contained file (no external scripts, fonts or styles) with the same summary, a tag matrix, a task matrix and a table of all runs.

## Getting tasks

### Mine them from history

```
taskreplay mine path/to/repo --since 2026-01-01 --test-cmd "python -m pytest -q {test_files}" --out tasks.yaml
```

`mine` looks at non-merge commits that change both source files and test files. For each candidate it creates two worktrees and runs the test command:

1. at the parent commit, with the commit's test files put in place: the tests must fail
2. at the commit itself: the tests must pass

Only candidates that pass both checks are written to `tasks.yaml`. `{test_files}` is replaced by the test files the commit changed. The prompt is the commit message without trailers such as `Signed-off-by`; **edit it** so it reads like the request you would give an agent. Commit messages are often too short ("fix bug") or give the solution away. Conventional commit prefixes become tags (`fix:` becomes `bugfix`, `feat:` becomes `feature`, also `refactor` and `performance`).

`taskreplay mine REPO --list` only lists candidates and runs no tests. `--limit`, `--max-scan` and `--ref` narrow the search.

### Write them by hand

```yaml
tasks:
  - id: parser-empty-input          # unique id
    repo: ../my-project             # relative to this file, or absolute
    base: 3f2c9a1                   # commit the agent starts from
    solution: 8be0d44               # optional: the real fix, used by `validate`
    prompt: |
      parse_config("") raises IndexError. It should return an empty Config.
    test: python -m pytest -q tests/test_parser.py
    allowed_files: ["src/parser/*.py"]   # optional, glob patterns
    hidden_tests:                   # optional: put in place after the agent finishes
      - path: tests/test_parser.py
        from_commit: 8be0d44        # take the file from this commit
      - path: tests/test_extra.py
        from_file: hidden/test_extra.py   # or copy a local file (relative to this file)
    timeout: 900                    # seconds for the agent (default 900)
    test_timeout: 300               # seconds for the test command (default 300)
    tags: [bugfix, parser]
```

`taskreplay validate tasks.yaml` checks every task: the test command must fail at `base` (after the hidden tests are placed) and pass at `solution` if one is given. A task whose tests already pass at `base` measures nothing.

Hidden tests are copied in after the agent has finished and before scoring. They overwrite anything the agent did to those files, so an agent cannot pass by deleting or weakening the test. With `allowed_files`, the prompt lists the allowed patterns, and a run that changes other files fails even if the tests pass.

## Runners

`--runners` takes names. `claude` and `codex` work without configuration. Everything else is defined under `runners:` in the task file or in a separate file passed with `--config`:

```yaml
runners:
  claude:                          # plain Claude Code
    type: claude
  claude-sonnet:
    type: claude
    model: sonnet
    allowed_tools: ["Bash(python -m pytest:*)"]   # let it run the tests
  codex:
    type: codex
    price_per_mtok_in: 1.25        # example prices: codex reports no cost, so
    price_per_mtok_out: 10.0       # taskreplay estimates it from tokens
  cheap-api:
    type: openai-compatible
    base_url: https://api.example.com/v1
    model: some-small-model
    api_key_env: EXAMPLE_API_KEY   # NAME of the environment variable, never the key
    price_per_mtok_in: 0.3
    price_per_mtok_out: 1.2
  local:
    type: openai-compatible
    base_url: http://localhost:1234/v1   # LM Studio; Ollama is http://localhost:11434/v1
    model: qwen2.5-coder-7b-instruct
  other-agent:
    type: command
    command: "other-agent --yes --message-file {prompt_file}"
```

| type | what it runs | usage data |
|---|---|---|
| `claude` | `claude -p --output-format json --permission-mode acceptEdits --no-session-persistence`, prompt on stdin, in the worktree | tokens, `total_cost_usd`, `num_turns` from the JSON result |
| `codex` | `codex exec --json --sandbox workspace-write --cd <worktree> --ephemeral -`, prompt on stdin | tokens from `turn.completed` events; turns = completed commands, file changes and messages |
| `openai-compatible` | built-in loop with the tools `read_file`, `write_file`, `run_command` | tokens from `usage`; cost from `usage.cost` if the gateway sends it, else from configured prices |
| `command` | any shell command in the worktree | optional JSON file `{usage_file}` with `tokens_in`, `tokens_out`, `cost_usd`, `turns`, `model` |
| `module:Class` | your own subclass of `taskreplay.runners.base.Runner` | whatever it returns |

Permissions are kept narrow. The Claude runner uses `acceptEdits`: file edits in the worktree go through, shell commands are refused unless you list them in `allowed_tools`. The Codex runner uses the `workspace-write` sandbox. Neither uses the bypass flags. Change this with `permission_mode`, `sandbox` or `extra_args` if you know what you are doing.

`price_per_mtok_in` and `price_per_mtok_out` (USD per million tokens) work on every runner and only apply when the agent reports no cost itself. The Claude CLI reports `total_cost_usd` even on a subscription; it is then a notional API price, not money you are billed.

Command runner placeholders: `{prompt_file}`, `{workdir}`, `{usage_file}`, `{timeout}`, `{task_id}`, `{attempt}`. Values are quoted for the platform shell. Write `{{` and `}}` for literal braces. The same values are exported as `TASKREPLAY_PROMPT_FILE`, `TASKREPLAY_WORKDIR`, `TASKREPLAY_USAGE_FILE`, `TASKREPLAY_TASK_ID`, `TASKREPLAY_ATTEMPT`. Options: `stdin: true` pipes the prompt on stdin, `env:` adds variables.

Built-in agent loop options: `max_steps` (default 40), `command_timeout` (60 s), `allow_commands` (true), `request_timeout` (180 s), `temperature`, `max_output_chars` (20000), `system_prompt`, `allow_secret_files` (list of glob patterns, see [Secrets](#secrets)).

## Running

```
taskreplay run tasks.yaml --runners claude,codex,cheap-api --attempts 2 --dry-run
taskreplay run tasks.yaml --runners claude,codex,cheap-api --attempts 2 --max-cost 5 --max-tasks 10
```

For every (task, runner, attempt) taskreplay:

1. creates a detached worktree of `base` in a new temporary directory
2. starts the runner there with the timeout from the task
3. stages all changes in the worktree's own index and records the diff against `base`
4. places hidden tests and runs the test command
5. writes one JSON line to `results/run-<timestamp>.jsonl`, the agent output to `results/logs/` and the diff to `results/patches/`
6. removes the worktree (keep it with `--keep`)

Runs go task by task, all runners on a task before the next task, so a budget stop leaves the runners with comparable coverage. `--max-cost` stops starting new runs once reported or estimated cost reaches the limit. The Claude runner also gets `--max-budget-usd` with the remaining budget, and the built-in loop stops itself when it crosses it. Runs with no cost information do not count toward the limit; taskreplay prints how many there were. `--only id1,id2` selects tasks.

Your checkout is never modified. `git worktree add` writes a small entry under `.git/worktrees/`, which is removed again with the worktree.

## Reports

```
taskreplay report                 # reads results/*.jsonl, writes results/report.html
taskreplay report results/run-20260930-101500.jsonl --html out.html --runners claude,codex
```

Columns: runs, pass rate, median wall time, median tokens (in + out), median turns, median diff lines, total cost, cost per solved task (total cost divided by passed runs), errors, timeouts. "Best runner per tag" picks the highest pass rate, then the lower cost per solved task, then the lower median time. With only a few tasks per tag, treat that as a hint.

## Use with Claude Code or Codex

taskreplay is a CLI, so either agent can call it. Useful patterns:

- Let the agent draft the task file: ask it to run `taskreplay mine . --since "3 months ago" --out tasks.yaml`, then rewrite the prompts in the generated file, then run `taskreplay validate tasks.yaml`.
- A Claude Code slash command, for example `.claude/commands/replay.md`:

  ```
  Run `taskreplay run tasks.yaml --runners $ARGUMENTS --dry-run`, show me the plan and the
  number of runs, and only start the real run after I confirm. Then run `taskreplay report`
  and summarise which runner to use for which tag.
  ```

- For Codex, put the same instructions in `AGENTS.md`.

When taskreplay itself runs inside a Claude Code session, it removes the `CLAUDECODE` variable from the environment of the `claude` child process so the child does not treat itself as nested. This was not tested with a live session. Both sessions use your quota.

## Secrets

The built-in `openai-compatible` loop keeps credentials away from the model:

- `read_file` refuses files that usually hold credentials: `.env` and `.env.*` (but not `*.example`, `*.sample`, `*.template`, `*.dist`), `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `*.kdbx`, SSH private keys such as `id_rsa` or `id_ed25519` (not `*.pub`), `.netrc`, `.git-credentials`, `.npmrc`, `.pypirc`, `.htpasswd`, `credentials`, `credentials.json`, `service-account*.json`, and everything under `.ssh/`, `.gnupg/`, `.aws/` and `.docker/`. The check applies to the requested name and to the target of a symlink. Paths that leave the worktree, also through a symlink, are refused as before. To let the model read such a file, list a glob pattern under `allow_secret_files`, for example `allow_secret_files: [".env.test"]`.
- `run_command` gets a copy of your environment without the variable named in `api_key_env` and without every variable whose name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `CREDENTIAL` or `PRIVATE`, plus a few known names such as `DATABASE_URL`. If the tests the agent runs need one of these variables, the agent does not see it. The scoring test command still runs with your full environment.
- Tool output is redacted before it is sent to the model or written to the transcript: PEM private key blocks, `Bearer ...` tokens, `Authorization: Basic ...`, `sk-...` keys, GitHub tokens (`ghp_...`, `github_pat_...`), AWS access key ids (`AKIA...`, `ASIA...`), Google API keys, Slack and Hugging Face tokens, passwords in URLs (`https://user:password@host`), the literal value of the configured API key, and the literal values (8 characters or more) of secret-looking environment variables. Each match becomes `[REDACTED]`.
- API requests never follow HTTP redirects. A 3xx answer ends the run with an error that names the target, so the `Authorization` header only goes to `base_url`. Point `base_url` at the final address.

For every runner, taskreplay applies the same redaction to the task prompt, the agent log in `results/logs/`, the JSON lines in `results/*.jsonl` (error messages, test output) and the error column of the HTML report. The limits are listed below.

## Limitations

- The agents in the worktree see your normal agent setup: the Claude runner loads your settings, `CLAUDE.md` files, hooks and plugins; Codex loads `~/.codex/config.toml`. That is realistic for "how does my setup do", but it means results differ between machines.
- Worktrees are not sandboxes. They share the object database and refs with your repository, so an agent that runs `git branch -D` or `git push` inside the worktree acts on your repository. The `run_command` tool of the built-in loop starts in the worktree but can reach anything your user can. Run untrusted models in a container or VM.
- Secret redaction works on patterns and known values. A password in a format none of the patterns knows, a secret split across lines, or an encoded secret passes through unchanged.
- The `run_command` tool can still read any file your user can read, for example `cat .env` or a file outside the worktree. Only the output redaction applies there. Set `allow_commands: false`, or run the loop in a container, when the repository or the machine holds secrets.
- The Claude, Codex and command runners pass your full environment to the agent, because those agents need their own credentials. What they send to their model is decided by the agent and its permission or sandbox settings, not by taskreplay.
- Patches in `results/patches/` are stored unchanged so that they still apply. If an agent writes a secret into a file, the secret is in the patch.
- The redaction of task prompts can change a prompt that deliberately contains a token-like example string.
- The test command runs in your current environment. If your project is installed in editable mode from the main checkout, tests in the worktree may import the main checkout's code. Use a command that imports from the working directory (`python -m pytest` in a flat or `src` layout with a matching pytest config) or set up a per-worktree environment inside the test command.
- The Codex event format (`turn.completed` usage, `item.completed` items) was implemented from the documented `codex exec --json` output and tested against recorded samples, not against a live Codex session. The Claude runner was likewise tested with recorded JSON and a stub executable. During development no paid agent or API was called; only `--help` of both CLIs was read.
- Turn counts are not comparable between runners: Claude reports model turns, the Codex runner counts completed items, the built-in loop counts API calls.
- Cost numbers are only as good as the source: Claude's reported cost is an API list price, estimates depend on your configured prices, cached tokens are counted at the normal input price.
- `mine` classifies test files by name and directory (for example `test_*.py`, `*_test.go`, `*.spec.ts`, `tests/`). Unusual layouts need hand-written tasks.
- Mined prompts come from commit messages and usually need editing.
- Few tasks and single attempts give noisy numbers. Agents are not deterministic; use `--attempts` of 2 or more for decisions.

## Development

```
python -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/python -m pytest -q
```

All tests use fake runners, local scripts and a synthetic git repository in a temp directory. The OpenAI-compatible loop is tested against a local fake HTTP server.

## License

MIT, see [LICENSE](LICENSE).
