# Tracing

A trace is an append-only JSONL file with one record per model call, tool call and decision. It works against any OpenAI-compatible endpoint and needs no server feature: what a server doesn't report is `null`, and unknown is never recorded as zero. Tracing doesn't change the request bodies (`tests/test_golden_requests.py` checks this).

## Turning it on

```bash
DCODE_TRACE=runs/a.jsonl dcode run --mode auto "the task"
DCODE_TRACE=runs/a.jsonl DCODE_TRACE_SNAPSHOTS=1 DCODE_TASK_ID=study-41 DCODE_EXPERIMENT=variant-b.json dcode run "the task"
```

Or in `~/.dcode/config.json`:

```json
{ "trace": { "path": "~/runs/a.jsonl", "snapshots": false } }
```

- **Privacy.** Records hold counts, sizes, hashes and short error messages, never prompts or file contents. The file is mode 0600, even if it already existed. A trace never holds an API key or URL credentials; a profile's server appears as its host name only.
- **Snapshots** hold real content. With `trace.snapshots` on, the exact request body of each call is stored as `<trace dir>/blobs/<sha256>.json` (0600, folder 0700) before anything can change it, and `llm_call.snapshot` names it. They contain prompts and file contents, but no key, since the key travels in a header.
- **Failures.** A trace error never stops a task. An unwritable path loses the records and the task goes on.
- **Shared files.** Each process writes its own `run_header` the first time it writes, so a file appended to by several runs has several headers. Group by `task_id` or split at each header. Writes from parallel helpers never mix within a line.
- **Task identity.** Every task gets a `task_id`, from `DCODE_TASK_ID` or generated. Every request carries it as the header `X-Dcode-Task`, even with tracing off, and the Dcode Manager stores it (`GET /api/admin/requests?task=<id>`). With `DCODE_TASK_ID` set, every task in that process shares the id.

## Every record

| Field | Meaning |
|---|---|
| `ts` | Unix time the record was written. |
| `task_id` | The task it belongs to. |
| `agent_id` | `main`, or a helper such as `explore-1`. `null` on `run_header`. |
| `type` | One of the record types below. |

## `run_header`

The first record a process writes.

| Field | Meaning |
|---|---|
| `dcode_version` | Dcode's version. |
| `dcode_git` | `{commit, dirty}` of Dcode itself. Both `null` for an installed package or binary. |
| `python` | Python version. |
| `platform` | Operating system and architecture. |
| `experiment` | Every effective experiment setting. |
| `experiment_hash` | SHA-256 of those settings: the same settings give the same hash. |
| `experiment_overrides` | Only what differs from the defaults. |
| `profiles` | One row per model: `name`, `model`, `base_url_host`, `context_window`, `max_output_tokens`, `family`, `reasoning_effort`. |
| `stream` | Whether replies are streamed. |
| `router` | The router setting (`auto`, `always`, `off`), or `null`. |
| `workspace` | The workspace path. |
| `snapshots` | Whether request bodies are being stored. |

## `llm_call`

One record per attempt, so a retried call has one record per try and their tokens add up.

| Field | Meaning |
|---|---|
| `purpose` | `main_step`, `helper_step`, `title`, `commit_msg`, `router`, `compaction_summary`, `final_report` or `other`. |
| `step` | The agent's step, counted from 0. `null` outside a step. |
| `attempt` | The try within this call, counted from 0. |
| `retry_reason` | Why this attempt is a repeat: `none`, `busy` (rate limited), `learned_param` (the server refused a field, which is now dropped), `think_cutoff` (asked again with less reasoning) or `context_overflow` (retried after compacting). |
| `model` | The model name sent. |
| `server_kind` | The server type Dcode detected. |
| `effort` | The reasoning effort for this attempt. |
| `max_tokens` | The output cap sent. |
| `message_count` | Messages in the request. |
| `tools_count` | Tool schemas in the request. |
| `estimated_prompt_tokens` | Dcode's estimate: characters / 3.5 with its message and tool framing. |
| `estimated_completion_tokens` | Characters received / 3.5. |
| `reported_prompt_tokens` | The server's figure, or `null`. |
| `reported_completion_tokens` | The server's figure, or `null`. |
| `reasoning_tokens` | `usage.completion_tokens_details.reasoning_tokens`, or `null`. |
| `cached_tokens` | `usage.prompt_tokens_details.cached_tokens`, or `null`. |
| `usage_estimated` | `true` when the attempt was answered but reported no usage, for example a stream cut off before its usage chunk. |
| `counted_prompt_tokens` | What totals use: the reported figure, else the estimate when `usage_estimated`, else 0 for a refused attempt. |
| `counted_completion_tokens` | The same for the completion. |
| `request_ts` | Unix time the request started. |
| `ttft_ms` | Time to the first delta, reasoning included. `null` without a stream. |
| `last_delta_ms` | Time to the last delta. `null` without a stream. |
| `decode_ms` | First delta to last delta. `null` without a stream. |
| `total_ms` | The whole attempt, as the client saw it. |
| `finish_reason` | The server's finish reason. For a mid-stream reasoning cut-off, why Dcode cut it. `null` if none arrived. |
| `streamed` | Whether the attempt was streamed. |
| `snapshot` | The blob's SHA-256, or `null`. |
| `rejected_fields` | Request fields this client has learned the server refuses, or `null`. |
| `http_sends` | HTTP requests under this attempt. Above 1 means the SDK retried by itself. |
| `http_statuses` | The status of each of those responses. |
| `error_class` | The exception's class, or `null`. |
| `error_message` | Its message, cut to 300 characters, or `null`. |

### Prefix stability

Computed by Dcode when the request is sent, so it needs nothing from the server and can check a server's `cached_tokens`.

| Field | Meaning |
|---|---|
| `message_hashes` | For each message, the first 16 hex characters of the SHA-256 of its canonical `{role, content, tool_calls}`. |
| `tools_hash` | The same for the tool list, or `null` without tools. |
| `prev_messages` | Messages in the previous call of the same chain, or `null` for the first. |
| `lcp_messages` | How many leading messages are unchanged. |
| `lcp_tokens` | Estimated tokens in that shared prefix, including the tool list when it is unchanged, because servers render tools first. 0 when the tools changed. |
| `first_changed_index` | Index of the first message that differs, or `null` while the prefix is intact. |
| `first_changed_role` | That message's role. `null` when the request simply got shorter. |
| `prefix_intact` | Whether all of the previous request's messages are still in place. `null` for the first. |
| `tools_changed` | Whether the tool list changed. `null` for the first. |

A chain is one agent's conversation: `main_step`, `helper_step` and `final_report` compare with the agent's previous such call. Each side job (`title`, `router`, `compaction_summary`, `commit_msg`, `other`) compares only with the previous job of its kind. Otherwise a title request would look like a broken prefix between two main steps that share everything.

`lcp_tokens` is an estimate and `cached_tokens` is what a server's own cache reused, so they answer different questions. A server can report less than the shared prefix when it has evicted the entry. A persistent gap with `prefix_intact: true` points at the server, not at Dcode.

## `tool_call`

| Field | Meaning |
|---|---|
| `step` | The agent's step. |
| `name` | The tool. |
| `arg_chars` | Size of its JSON arguments. |
| `raw_output_chars` | Output size before caps. `null` when a tool cut its output and can't say how long it was. |
| `kept_chars` | Size kept after caps and truncation. |
| `truncated` | Whether anything was cut. |
| `duration_ms` | Time in the tool, without the wait for approval. |
| `approval_wait_ms` | Time waiting for the user. |
| `ok` | `false` for a refusal or a failure. |
| `error` | `return_code`, `timeout` or `tool_error`; `null` otherwise. Never the output itself. |
| `was_refused` | Dcode refused it before it ran. |
| `refusal` | Why: `repeat_guard`, `blind_write`, `file_rule`, `command_rule`, `plan_mode`, `commit_gate`, `hook`, `approval_denied`, `invalid_arguments` or `unavailable`. |

## `compaction`

| Field | Meaning |
|---|---|
| `stage` | The stage reached: 1 stubs old outputs and arguments, 2 summarizes, 3 drops the oldest turns. |
| `trigger` | `auto`, or `forced` (`/compact` or a server's overflow refusal). |
| `before_tokens` | Estimated prompt size before. |
| `after_tokens` | Estimated prompt size after. |
| `elided` | Tool results stubbed. |
| `summarized` | Messages folded into the summary. |
| `summary_tokens` | Estimated size of the summary. |
| `args_shrunk` | Old calls whose arguments were stubbed. |
| `intra_task` | The summary covered steps of the running task. |

A summary's model call appears as its own `llm_call` with purpose `compaction_summary`.

## `elision`

Written at the start of each main-agent task, even when nothing was stubbed.

| Field | Meaning |
|---|---|
| `count` | Outputs of earlier turns stubbed. |
| `chars_removed` | Characters that took out of the history. |

## `elision_regret`

The model called a tool again with the same arguments as a call whose output had left its context. This is the direct measure of what a stub or summary cost.

| Field | Meaning |
|---|---|
| `name` | The tool. |
| `how` | `elided` or `summarized`. |
| `signature` | Hash of the tool name and arguments. |

## `route`

One per routed task, whether or not it changed the model.

| Field | Meaning |
|---|---|
| `source` | What settled it: `off`, `always`, `follow_up`, `utility` or `heuristic`. |
| `reason` | The reason in words; empty when the task stays on the main model. |
| `hard` | Whether the router wanted the reasoning model. |
| `model` | The model chosen. |
| `utility_answer` | The utility model's raw answer, or `null` if it wasn't asked or failed. |
| `input_chars` | The task's length. |

## `delegate`

| Field | Meaning |
|---|---|
| `role` | The helper's role. |
| `helper_id` | Its `agent_id`, which its own records carry. |
| `model` | Its model. |
| `ok` | Whether it finished without failing. |
| `steps` | Rounds of tool calls it ran. |
| `report_chars` | Report size before the cap. |
| `report_kept_chars` | Report size that reached the main context. |
| `calls` | Attempts it made. |
| `prompt_tokens` | Its prompt tokens. |
| `completion_tokens` | Its completion tokens. |
| `cached_tokens` | Its cached tokens, or `null`. |

## `task_end`

| Field | Meaning |
|---|---|
| `reason` | `final_answer`, `step_limit`, `stuck_guard`, `error`, `interrupted` or `context_overflow`. |
| `steps` | Rounds of tool calls. The closing reply isn't a step. |
| `calls` | Attempts in this task, including helpers and side jobs. |
| `estimated_calls` | How many of them have estimated usage. |
| `tool_calls` | Tool calls, including helpers'. |
| `model_ms` | Total time in model calls. Parallel helpers overlap, so this can exceed `wall_ms`. |
| `tool_ms` | Total time in tools, without `delegate`, whose time is already in its helper's calls and tools. |
| `approval_wait_ms` | Total time waiting for the user. |
| `wall_ms` | The task from start to end. |
| `prompt_tokens` | The sum of `counted_prompt_tokens` over the task's attempts. |
| `completion_tokens` | The same for completions. |
| `cached_tokens` | The sum of what servers reported, or `null` if none did. |

## How totals relate

- `task_end` sums the `llm_call` records with its `task_id`, so it covers helpers, retries, titles and summaries. A traced task waits for its title job before ending, so that job lands in the total.
- With tracing on, the `usage` event and `stats.jsonl` match `task_end` for a task that finishes.
- With tracing off, a title job that ends after its task rolls into the next task's `usage` total. It is delayed, not lost.
- A failed or interrupted task emits no `usage` event. Its tokens roll into the next task's `usage` total. Its own `task_end` still reports them, so summing `task_end` records is the safe way to total a run.

## Example

A real run against the mock server (`docs/dev/mock_llm.py`), no model involved:

```bash
python docs/dev/mock_llm.py 18099 --simulate-cache --max-context 7488 --long-chars 2400 &
DCODE_BASE_URL=http://127.0.0.1:18099/v1 DCODE_API_KEY=x DCODE_MODEL=mock \
DCODE_CONTEXT_WINDOW=8000 DCODE_MAX_OUTPUT_TOKENS=512 \
DCODE_TRACE=trace.jsonl DCODE_TASK_ID=demo-1 DCODE_EXPERIMENT=exp.json \
dcode run --mode auto "long task 12"
```

`exp.json` is `{"recall_placement":"user","seed":7}`. The lines below are from that run. For reading, they leave out `message_hashes`, the bulkier header fields and fields that are `null`:

```json
{"ts":1793961155.3434458,"task_id":"demo-1","type":"run_header","dcode_version":"0.7.1","experiment_hash":"1d2ab8232e63ee2f49474eb30a1c128e4ed3001b030acf2361ec4e67f5427b28","experiment_overrides":{"recall_placement":"user","seed":7},"profiles":[{"name":"main","model":"mock","base_url_host":"127.0.0.1","context_window":8000,"max_output_tokens":512,"family":"auto","reasoning_effort":"medium"}],"stream":true,"router":"auto","snapshots":false}
{"ts":1793961155.36,"task_id":"demo-1","agent_id":"main","type":"route","source":"off","reason":"","hard":false,"model":"mock","input_chars":12}
{"ts":1793961155.361,"task_id":"demo-1","agent_id":"main","type":"elision","count":0,"chars_removed":0}
{"ts":1793961155.595,"task_id":"demo-1","agent_id":"main","type":"llm_call","purpose":"main_step","step":0,"attempt":0,"retry_reason":"none","model":"mock","server_kind":"local","effort":"medium","max_tokens":512,"message_count":2,"tools_count":23,"estimated_prompt_tokens":2816,"estimated_completion_tokens":767,"reported_prompt_tokens":2974,"reported_completion_tokens":770,"cached_tokens":0,"usage_estimated":false,"counted_prompt_tokens":2974,"counted_completion_tokens":770,"request_ts":1793961155.37,"ttft_ms":61.5,"last_delta_ms":183.2,"decode_ms":121.7,"total_ms":223.5,"finish_reason":"tool_calls","streamed":true,"http_sends":1,"http_statuses":[200],"tools_hash":"5b0722c66b72afff","lcp_messages":0,"lcp_tokens":0}
{"ts":1793961155.603,"task_id":"demo-1","agent_id":"main","type":"tool_call","step":0,"name":"write_file","arg_chars":2686,"raw_output_chars":29,"kept_chars":29,"truncated":false,"duration_ms":6.6,"approval_wait_ms":0.0,"ok":true,"was_refused":false}
{"ts":1793961155.797,"task_id":"demo-1","agent_id":"main","type":"llm_call","purpose":"main_step","step":1,"attempt":0,"retry_reason":"none","model":"mock","server_kind":"local","effort":"medium","max_tokens":512,"message_count":4,"tools_count":23,"estimated_prompt_tokens":3609,"estimated_completion_tokens":767,"reported_prompt_tokens":3883,"reported_completion_tokens":770,"cached_tokens":2974,"usage_estimated":false,"counted_prompt_tokens":3883,"counted_completion_tokens":770,"request_ts":1793961155.604,"ttft_ms":29.2,"last_delta_ms":151.2,"decode_ms":122.1,"total_ms":191.7,"finish_reason":"tool_calls","streamed":true,"http_sends":1,"http_statuses":[200],"tools_hash":"5b0722c66b72afff","prev_messages":2,"lcp_messages":2,"lcp_tokens":2816,"prefix_intact":true,"tools_changed":false}
{"ts":1793961155.807,"task_id":"demo-1","agent_id":"main","type":"tool_call","step":1,"name":"write_file","arg_chars":2686,"raw_output_chars":29,"kept_chars":29,"truncated":false,"duration_ms":8.7,"approval_wait_ms":0.0,"ok":true,"was_refused":false}
{"ts":1793961156.004,"task_id":"demo-1","agent_id":"main","type":"llm_call","purpose":"main_step","step":2,"attempt":0,"retry_reason":"none","model":"mock","server_kind":"local","effort":"medium","max_tokens":512,"message_count":6,"tools_count":23,"estimated_prompt_tokens":4402,"estimated_completion_tokens":767,"reported_prompt_tokens":4801,"reported_completion_tokens":770,"cached_tokens":3883,"usage_estimated":false,"counted_prompt_tokens":4801,"counted_completion_tokens":770,"request_ts":1793961155.809,"ttft_ms":30.0,"last_delta_ms":152.5,"decode_ms":122.5,"total_ms":192.8,"finish_reason":"tool_calls","streamed":true,"http_sends":1,"http_statuses":[200],"tools_hash":"5b0722c66b72afff","prev_messages":4,"lcp_messages":4,"lcp_tokens":3609,"prefix_intact":true,"tools_changed":false}
{"ts":1793961156.008,"task_id":"demo-1","agent_id":"main","type":"tool_call","step":2,"name":"write_file","arg_chars":2686,"raw_output_chars":29,"kept_chars":29,"truncated":false,"duration_ms":3.3,"approval_wait_ms":0.0,"ok":true,"was_refused":false}
{"ts":1793961156.204,"task_id":"demo-1","agent_id":"main","type":"llm_call","purpose":"main_step","step":3,"attempt":0,"retry_reason":"none","model":"mock","server_kind":"local","effort":"medium","max_tokens":512,"message_count":8,"tools_count":23,"estimated_prompt_tokens":5195,"estimated_completion_tokens":767,"reported_prompt_tokens":5718,"reported_completion_tokens":770,"cached_tokens":4800,"usage_estimated":false,"counted_prompt_tokens":5718,"counted_completion_tokens":770,"request_ts":1793961156.009,"ttft_ms":31.5,"last_delta_ms":153.7,"decode_ms":122.2,"total_ms":194.2,"finish_reason":"tool_calls","streamed":true,"http_sends":1,"http_statuses":[200],"tools_hash":"5b0722c66b72afff","prev_messages":6,"lcp_messages":6,"lcp_tokens":4402,"prefix_intact":true,"tools_changed":false}
{"ts":1793961156.214,"task_id":"demo-1","agent_id":"main","type":"compaction","stage":1,"trigger":"auto","before_tokens":6384,"after_tokens":5614,"elided":0,"summarized":0,"summary_tokens":0,"args_shrunk":1,"intra_task":false}
{"ts":1793961156.409,"task_id":"demo-1","agent_id":"main","type":"llm_call","purpose":"main_step","step":4,"attempt":0,"retry_reason":"none","model":"mock","server_kind":"local","effort":"medium","max_tokens":512,"message_count":10,"tools_count":23,"estimated_prompt_tokens":5266,"estimated_completion_tokens":767,"reported_prompt_tokens":5847,"reported_completion_tokens":770,"cached_tokens":3024,"usage_estimated":false,"counted_prompt_tokens":5847,"counted_completion_tokens":770,"request_ts":1793961156.214,"ttft_ms":30.5,"last_delta_ms":151.9,"decode_ms":121.4,"total_ms":193.1,"finish_reason":"tool_calls","streamed":true,"http_sends":1,"http_statuses":[200],"tools_hash":"5b0722c66b72afff","prev_messages":8,"lcp_messages":2,"lcp_tokens":2816,"first_changed_index":2,"first_changed_role":"assistant","prefix_intact":false,"tools_changed":false}
{"ts":1793961158.026,"task_id":"demo-1","agent_id":"main","type":"task_end","reason":"final_answer","steps":12,"calls":13,"estimated_calls":0,"tool_calls":12,"model_ms":2514.9,"tool_ms":82.8,"approval_wait_ms":0.0,"wall_ms":2683.6,"prompt_tokens":74620,"completion_tokens":9250,"cached_tokens":43458}
```

Reading it:

- Steps 1 to 3 only append, so `prefix_intact` is `true` and the simulated `cached_tokens` grows with each step.
- The `compaction` line is stage 1 stubbing one old `write_file`.
- On the next call `first_changed_index` is 2 and its role is `assistant`: that is the call whose arguments were just shrunk, and `cached_tokens` falls from 4,800 to 3,024.
- `lcp_tokens` runs below `cached_tokens` here because the mock also counts JSON punctuation. Compare trends rather than single values.

## Reading a trace

```python
import json
rows = [json.loads(line) for line in open("trace.jsonl")]
calls = [r for r in rows if r["type"] == "llm_call"]
broken = [(r["step"], r["first_changed_role"]) for r in calls if r["prefix_intact"] is False]
retries = [r for r in calls if r["attempt"] > 0 or (r["http_sends"] or 0) > 1]
```
