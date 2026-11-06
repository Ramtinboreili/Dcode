# The benchmark

42 tasks that measure what each of Dcode's context mechanisms costs and buys. A runner executes every task × arm × model × repeat, scores each run with a hidden check, and an analysis turns the folder into a report. It works with any OpenAI-compatible endpoint. The harness and the checks use the Python standard library only.

| Category | Easy | Medium | Hard | Total |
|---|---:|---:|---:|---:|
| coding | 5 | 7 | 4 | 16 |
| devops | 6 | 7 | 5 | 18 |
| long | 2 | 3 | 3 | 8 |
| **Total** | **13** | **17** | **12** | **42** |

## Before a real run

**There is no sandbox.** The agent runs with `--mode auto`, so it executes whatever commands the model chooses, and the checks execute what the agent wrote. Run the benchmark in a VM or a container that holds nothing you care about, and never on a host with cluster or production access.

The runner reduces the risk but doesn't remove it. The agent's environment is built from nothing: `HOME` and `TMPDIR` are temporary, `KUBECONFIG=/dev/null`, `DOCKER_HOST=unix:///nonexistent`, no `SSH_AUTH_SOCK`, and only allowlisted variables. The agent can still read any file your user can read, including `~/.kube/config` by its absolute path, and can reach the network.

- **The API key.** The agent's commands can read it from `DCODE_API_KEY`, as they can in normal use. Use a key limited to this study. The runner writes it nowhere, and if it appears in a run's files it's redacted, `key_seen` is set and the report tells you to replace it.
- **Real tools in checks.** Checks use `docker build`, `helm template`, `nginx -t` and `ansible-playbook --syntax-check` when they're installed. `docker build` runs with `--pull=false --network=none` against the agent's Dockerfile on your Docker daemon. Set `BENCH_NO_REAL_TOOLS=1` to use only the static asserts. Results with and without real tools aren't comparable, so keep this the same across a study.
- **The hidden checks.** The runner needs `bench/tasks/<id>/check/` on disk, so an agent that searched for it could find it. Look for that afterwards: `grep -l 'bench/tasks' results/study1/runs/*/*/*/*/stdout.log`.

## Running it

```bash
make bench-validate     # the oracle: every check fails on its repo and passes with its solution
make bench-smoke        # the whole pipeline against the mock server, about 15 seconds

cp bench/models.example.json my-models.json    # then fill in your endpoint
export BENCH_API_KEY=...                       # the variable named by api_key_env
python bench/run.py --models my-models.json --repeats 3 --out results/study1 --seed 1
python bench/analyze.py --out results/study1
```

| Flag of `bench/run.py` | Meaning |
|---|---|
| `--tasks` | Ids, globs or categories: `--tasks coding 'k8s-*' long-kv-store`. Default: all. |
| `--arms` | The arms file. Default: `bench/arms.json`. |
| `--models` | The profiles file. Default: `bench/models.json`, which is only the mock. Every profile in the file runs. |
| `--repeats N` | Runs of each combination. |
| `--out DIR` | Where results go. |
| `--parallel N` | Worker slots, each running one agent at a time. Keep it at 1 unless the server has parallel capacity: slots share its queue and its prefix cache. |
| `--timeout-scale X` | Multiplies each task's `timeout_s`, for slow servers. |
| `--dcode` | `source` (this checkout, the default) or the path of a binary. |
| `--resume` | Skip runs that have a `result.json`. |
| `--retry-errors` | With `--resume`, also redo runs whose status is `error`. |
| `--max-steps N` | Overrides each task's `max_steps`. |
| `--seed N` | Sends sampling seed `N + repeat`, the same for all arms of a repeat. It needs a server that honours it. |
| `--workdir DIR` | Holds the fixed workspace paths. Default: `/tmp/dcode-bench`. One runner at a time per folder. |

A profile in the models file:

```json
{ "qwen-32k": { "base_url": "http://gpu-host:8010/v1", "model": "qwen3-coder", "api_key_env": "BENCH_API_KEY",
                "context_window": 32768, "max_output_tokens": 4096 } }
```

`api_key_env` is the name of a variable, never the key. `null` sends a dummy key. If a variable is missing, the runner stops before running anything. `family` and `reasoning_effort` are optional.

### Stopping and resuming

Ctrl-C kills the running agents, keeps every finished run and writes `results.csv`. `--resume` continues: `result.json` is written last and whole, so a folder without one is cleared and redone. A `skipped` run is redone once its tool is there. An `error` run stays until `--retry-errors`, so a server that went down can be retried without repeating real results.

`--resume` refuses to continue if a task, arm or model changed, since the folder would then mix setups. Dcode's own commit is recorded in each result, and the report warns if a folder holds several. Adding repeats to a finished folder works: `--resume --repeats 5`.

## What each run does

1. Starts a fresh `DCODE_HOME`, with the arm's `seed_home` copied in if it has one.
2. Copies `repo/` to `<workdir>/w<slot>/repo` and makes one commit with a fixed author and date. A task stays on one slot, so all its arms get the same path and the same commit hash: both are in the system prompt.
3. Runs `dcode run --mode auto` with the task id, trace, experiment file, step limit and summary path.
4. At the timeout, sends SIGINT and allows 15 seconds for Dcode to write its records, then SIGKILL. Anything the agent left running is killed.
5. Saves `git diff` as `diff.patch`, copies the workspace to a clean folder, adds `check/` and runs `check.sh`. A `check/` folder the agent wrote is replaced.
6. Writes `result.json`.

**Order.** Runs of one task and repeat stay together, and the arm order rotates with the task and the repeat. A server's prefix cache favours whatever ran just before, so no arm should always go first. `order_index` records the position.

### Statuses

| Status | Meaning | In the pass rate? |
|---|---|---|
| `pass` | The check passed, even if Dcode exited non-zero. | Yes |
| `fail` | The check failed. This includes `context_overflow`, `step_limit` and `stuck_guard`: those are the arm's own results. | Yes |
| `timeout` | Hit the time limit. The check still runs, and `check_passed` records the result, but it counts as a failure. | Yes, as a failure |
| `error` | Dcode or the server failed: exit 2, a crash, or an exit with reason `error` and no passing check. | No, counted separately |
| `skipped` | A tool in `requires` is missing. Nothing was spent. | No, counted separately |

An overloaded server and a request an arm made too big can both end in `error`, so leaving errors out can flatter an arm. The report warns when error rates differ by more than 5 points between arms. Read `stderr.log` before trusting such a comparison.

## Output

```
results/study1/
  plan.json             every run in order, arms, models (host only, no key), Dcode's commit
  results.csv           one row per run
  report.md             the report
  arm_summary.csv  paired.csv  prefix.csv  regret.csv
  plots/                pass rate and token difference per model, if matplotlib is installed
  runs/<model>/<arm>/<task>/r<repeat>/
    result.json  trace.jsonl  summary.json  stdout.log  stderr.log  diff.patch  check.log  experiment.json
```

**How the report computes things:**

- **Confidence intervals** are a 95% percentile bootstrap with 2000 resamples of whole tasks. Repeats of a task aren't independent, and resampling runs would make three repeats look like three times the evidence. An interval needs at least two tasks. The seed comes from the comparison's name, so a folder always gives the same report.
- **Tokens per pass** is all tokens of counted runs divided by the passes, so failures are part of what a success costs.
- **Paired differences** compare an arm with its control on the same model, task and repeat. A pair with a `skipped` or `error` side is left out.
- **LCP ratio** is `lcp_tokens / estimated_prompt_tokens` per call, for calls that follow another. It comes from the requests, so it holds on servers that report no cached tokens.
- **Regret rate** is `elision_regret` records divided by stubbed outputs (`compaction.elided` + `elision.count`).

## Arms

An arm in `bench/arms.json`:

```json
{ "name": "window_16k", "experiment": {}, "context_window": 16384 }
```

| Field | Meaning |
|---|---|
| `name` | Used as a folder name. |
| `experiment` | Overrides from [experiment-config.md](experiment-config.md). A typo stops the runner before anything runs. |
| `context_window`, `max_output_tokens` | Override the model's values. |
| `seed_home` | A folder under `bench/` copied into the fresh `DCODE_HOME`. |
| `control` | The arm to compare with. Default: `baseline`. |
| `note` | Free text. |

To add an arm, add an object to the list. Results of the other arms stay valid and `--resume` runs only the new one. That arm then ran later than the others, on a server that may have changed, so for a comparison that matters start a new folder.

**The recall arms.** `recall_user` moves recalled memories from the system prompt to the user message. A fresh home has no memories, so it would behave exactly like `baseline`. Both recall arms therefore start from `bench/seed_homes/memories`, and `recall_user` compares with `recall_system`, which has the same memories in the default place. The memories are generic habits written for this benchmark, so they measure the placement, not how useful a real user's memories are.

## Adding a task

```
bench/tasks/<id>/
  task.json        id, category, difficulty, prompt, timeout_s, requires, tags, and optionally max_steps
  repo/            the starting files
  check/check.sh   exit 0 = pass. Runs in a clean copy of the final workspace.
  check/...        anything else the check needs. The agent never sees this folder.
  solution.patch   a known good answer, for the oracle only
```

```json
{ "id": "py-duration-parse", "category": "coding", "difficulty": "easy", "timeout_s": 300,
  "prompt": "parse_duration gives wrong numbers for things like 1h30m. fix please",
  "requires": ["python3"], "tags": ["python", "bugfix"] }
```

- **Prompts** read like a real user: short, natural, not a spec.
- **Checks** must be deterministic and offline. A check that passes on an obviously right answer written another way matters as much as one that fails on a wrong one: accept what the prompt allows, and avoid pinning names or formats it doesn't state.
- **`requires`** lists the tools the task can't work without. A missing one makes the run `skipped`, never failed. Optional real tools go through `benchcheck.tool_enabled("helm")`, with static asserts as the fallback.
- **The oracle** must pass: `python bench/validate_tasks.py --tasks <id> --verbose` runs the check on the untouched repo (must fail) and twice with the solution (must pass both times).

`bench/lib/benchcheck.py` is on the check's `PYTHONPATH`:

```python
from benchcheck import dig, finish, load_yaml, ok

dep = load_yaml("k8s/deployment.yaml")
ok(dig(dep, "spec", "replicas") == 2, "two replicas")
finish()    # exit 1 if any ok() failed
```

**YAML without PyYAML.** Checks parse YAML with `bench/lib/miniyaml.py`, a vendored subset: block collections, one-line flow collections, quoted strings, comments, multiple documents and block scalars. `yes`, `no` and `on` stay strings, as in YAML 1.2. It raises `MiniYamlError` on anchors, aliases, tags, merge keys, complex keys and plain scalars that continue on the next line, and the check then fails. Vendoring keeps the checks stdlib-only. The cost is that an agent's valid YAML using those features fails a check, so look for `MiniYamlError` in `check.log` when reading failures.

## Threats to validity

- **The task set was written by the project's owner**, with an AI assistant, alongside the tool it measures. It reflects their idea of DevOps work and may suit Dcode's design. Treat absolute pass rates as specific to this set. Differences between arms carry over better, but not automatically.
- **Small n.** 42 tasks is few, and the 8 long tasks, where compaction matters most, are fewer. Intervals will be wide. A difference inside them hasn't been shown, and with 12 arms some comparisons will look real by chance: no correction for multiple comparisons is applied.
- **Sampling is nondeterministic.** Even with `--seed`, many servers aren't reproducible across batches or restarts. Use at least 3 repeats. Repeats don't add tasks, so they narrow noise, not the limits of the task set.
- **Shared-server noise.** Other traffic moves time measurements and can evict a prefix cache, which changes cached tokens. Use a dedicated server for timing and cache results. Token counts of a given request aren't affected, but a slow server can turn into timeouts, so scale them.
- **Order effects.** Rotation balances which arm runs first but doesn't remove the effect, since one arm's requests can warm the cache for the next.
- **Checks are imperfect.** A check can reject a valid solution written in an unexpected way, or accept a weak one. The oracle only shows each check separates the untouched repo from one known solution. Sample `diff.patch` from failures before drawing conclusions.
- **Arms interact.** Each arm changes one mechanism against the defaults, so this says nothing about combinations.
- **`window_*` arms change what Dcode believes.** They set `context_window` on Dcode's side. To test a real server limit, also start the server with that limit and use a separate profile.
- **The agent can reach the network and the disk.** Nothing stops a model from looking things up, or finding a check. See [Before a real run](#before-a-real-run).
