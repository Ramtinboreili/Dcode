# Experiment config

The `experiment` section names every mechanism a study may switch or tune. With no section, Dcode runs on the defaults below. It then sends the same request bytes as before this section existed, except where the long-task fixes described below apply (`tests/test_golden_requests.py` compares them with a recording).

## Setting it

In `~/.dcode/config.json`:

```json
{ "experiment": { "recall_placement": "user", "seed": 7 } }
```

Or for one run, from a JSON file that overlays the config's section:

```bash
DCODE_EXPERIMENT=variant-b.json dcode run --mode auto "the task"
```

- An environment file is never written back to the saved config, and neither is `DCODE_TRACE`.
- An unknown key, a wrong type, an out-of-range number or an unreadable file stops the run with exit code 2. A typo must not quietly measure the baseline.
- `compact_target` may not be above `compact_trigger`.
- Each trace's `run_header` holds the effective section, only what differs from the defaults (`experiment_overrides`) and a SHA-256 of the effective settings (`experiment_hash`). Writing a default out or changing key order doesn't change the hash.

## Keys

### History

| Key | Default | What it changes |
|---|---:|---|
| `earlier_turn_elision` | `true` | When a new turn starts, big tool outputs of turns the model already answered become a stub. `false` keeps them until compaction. |
| `earlier_turn_min_chars` | `1500` | The output size at which that stub applies. |

### Compaction

| Key | Default | What it changes |
|---|---:|---|
| `compact_trigger` | `0.75` | Share of the input budget at which compaction starts. |
| `compact_target` | `0.45` | Share of the input budget compaction aims to leave. Stage 2 keeps fewer recent turns when it is lower. |
| `keep_recent_tool_results` | `3` | How many recent tool results and tool calls stage 1 leaves alone. |
| `elide_min_chars` | `600` | Tool results shorter than this are never stubbed by stage 1. |
| `summarization` | `true` | `false` skips stage 2, so nothing is sent to the model for a summary. Stage 3 can still drop the oldest turns. |

### Long tasks

| Key | Default | What it changes |
|---|---:|---|
| `shrink_old_args` | `true` | Stage 1 also stubs big arguments of old calls. `false` restores the old behaviour. |
| `shrink_args_tools` | `["write_file", "edit_file"]` | Which tools' calls may be shrunk. Only their `content`, `instructions`, `old_text` and `new_text` arguments are cut; a path stays readable. |
| `shrink_args_min_chars` | `240` | Arguments shorter than this stay whole. |
| `intra_task_summarize` | `true` | Stage 2 may summarize older steps inside the running task, cutting only between complete call and result groups. The task's own wording is pinned in the summary. `false` restores the old behaviour. |
| `intra_task_trigger` | `1.0` | The share of the input budget that must still be in use after the other cuts before older steps are summarized. |
| `overflow_recovery` | `true` | When the server says the prompt is too long: compact hard (at least halving the prompt) and retry once. A second refusal ends the task with reason `context_overflow`. `false` fails on the first refusal, as before. |

### Stored payloads

| Key | Default | What it changes |
|---|---:|---|
| `payload_stubbing` | `true` | Big arguments of calls whose payload is stored elsewhere become a stub right after the call. |
| `payload_stub_tools` | `["artifact_save", "skill_save"]` | Which tools that covers. Adding `write_file` and `edit_file` is the study variant that stubs file bodies immediately; the stub then says the tool result decides whether it worked. |
| `payload_stub_min_chars` | `240` | Arguments shorter than this stay whole. |

### Output cap

The cap on one tool's output is `context_window * 3.5 * output_cap_share` characters, held between the floor and the ceiling.

| Key | Default | What it changes |
|---|---:|---|
| `output_cap_share` | `0.12` | The share of the window. |
| `output_cap_min` | `3000` | The floor, in characters. |
| `output_cap_max` | `60000` | The ceiling, in characters. |

### Prompt size

| Key | Default | What it changes |
|---|---:|---|
| `skills_mode` | `"index"` | `"inline"` puts every skill's full text in the system prompt instead of a name index. |
| `mcp_schemas` | `"names"` | `"full"` sends each MCP tool as a real function schema instead of names in the prompt plus `mcp_tools` and `mcp_call`. |
| `recall_placement` | `"system"` | `"user"` puts recalled memories at the start of the latest user message. The system prompt then stays stable across tasks, so the prefix survives. |

### Reasoning cut-off

| Key | Default | What it changes |
|---|---:|---|
| `think_share` | `0.75` | The share of `max_tokens` (at 3.5 characters per token) a streamed reply may spend on reasoning before it is cut off and asked again with less. |
| `think_retries` | `2` | How many times a call may be asked again with less reasoning. |
| `loop_check_every` | `2000` | How many reasoning characters pass between checks for circular or overlong thinking. `0` turns the checks off. |

### Guards and follow-through

| Key | Default | What it changes |
|---|---:|---|
| `repeat_limit` | `3` | The identical call at this count in a row is refused by the repeat guard. A command is refused only if its earlier runs failed the same way. |
| `stuck_limit` | `2` | After this many repeat-guard refusals in a task, tools are taken away and the model must report. The task ends with reason `stuck_guard`. |
| `main_max_steps` | `60` | The main agent's step limit. Reaching it asks for a wrap-up and ends with reason `step_limit`. Helpers keep `agents.max_steps`. |
| `max_follow_ups` | `2` | How many times the model is reminded to follow through on a promised action in one task. |

### Other mechanisms

| Key | Default | What it changes |
|---|---:|---|
| `text_tool_recovery` | `true` | `false` stops parsing tool calls out of the reply's text, so such a reply stays plain text. |
| `delegation` | `true` | `false` removes the `delegate` tool and its prompt block. |

### Sampling

Sent only when set. A server that refuses one loses only that field, for the rest of the session.

| Key | Default | What it changes |
|---|---:|---|
| `temperature` | `null` | Request field `temperature`. |
| `top_p` | `null` | Request field `top_p`. |
| `seed` | `null` | Request field `seed`. |

## Why the long-task keys default to on

`shrink_old_args`, `intra_task_summarize` and `overflow_recovery` are on because the old behaviour fails on a long task: nothing could shrink the calls that wrote the code, turn boundaries can't help inside one turn, and a refused request ended the task.

They change requests only where the old code was failing or about to:

- **`intra_task_trigger: 1.0`** fires only when the prompt is still over the input budget after every other cut. Below that the request fits, so it stays as it was. A study can lower it to compare earlier summarizing.
- **`shrink_old_args`** applies once compaction runs and old calls hold big arguments. This is the one default that can change a request that would still have fitted. Set it to `false` for the earlier behaviour.
- **`overflow_recovery`** acts only after a server has refused a request.

To reproduce the old long-task requests byte for byte, set `{"shrink_old_args": false, "intra_task_summarize": false}`. The `long-writes` golden scenario checks this.

On a very small window, expect a summary on nearly every late step of a task with big writes, since one write can be a large share of the budget. On the mock server, with a 6,000-token window and writes of about 1,000 tokens, one 30-file task took 53 model calls and finished normally.

## One more change outside these keys

Output from a `!command` the model hasn't read yet is no longer stubbed, whatever the keys say. It was a bug: the model never saw the output it was asked about. Once the model has answered, the output is stubbed as usual.
