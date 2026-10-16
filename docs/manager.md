# Dcode Manager

One place for a team's models, users, limits and usage. The manager is a **model gateway**
(OpenAI-compatible `/v1`) in front of every model server, plus a web UI. Dcode signs in to it and
gets an API key and the models the user may use.

```
 Dcode (laptop, server) ──► Dcode Manager ──► vLLM gemma4   (vllm namespace)
 other OpenAI clients   ──►   /v1 gateway ──► vLLM qwen3
                              /api + UI   ──► OpenRouter, any OpenAI-compatible server
```

Same code base and binary as Dcode (`dcode manager serve`), standard library only (`http.server` +
SQLite), no internet needed.

## For users

**Connect Dcode.** On a new install, run `dcode` and choose *Sign in to a Dcode Manager*, or:

```bash
dcode login http://dcode-manager.corp:31010 -u sara
```

Dcode asks for your password, gets a key for this device, lists your models (pick one), and sets
the model's context length for you. Your other settings stay. `/model` switches between the
manager's models; `/usage` shows your tokens, limits and when they reset:

```
❯ /usage
  Usage  · sara on http://dcode-manager.corp:31010
    All models: 37,470 tokens of 500,000 (7%) · 462,530 left · resets Sun 01 Nov 00:00
    gemma4: 13,006 tokens of 13,000 (100%) · 0 left · resets Sat 17 Oct 00:00
```

**In the web UI** (the same address) you see your usage per model, make and revoke API keys for
scripts or other tools (base URL `<manager>/v1`), and how to connect.

## For admins

| Page | What you do there |
|---|---|
| **Monitoring** | Requests, tokens in/out, users, errors and refusals; tokens over time per model; each model's p50/p95 latency, time to first token, tokens/s, error rate; whether its server answers right now (and vLLM's running/waiting requests and KV-cache use when a metrics URL is set); top users; recent problems; devices (revoke a key). |
| **Models** | Add a model by hand or from Discovery. Turn it on/off. Its server URL, the name its server uses, its key, family, context length (longer requests are refused the way vLLM words it, so Dcode compacts), max reply, who may use it, the default token limit per user, guards, and its Kubernetes deployment (start/stop, change `--max-model-len`). *Test it* sends one tiny request. |
| **Discovery** | Model servers on the cluster (deployments and their services in the namespaces you choose; one entry per service port) and model weights in the models folder. *Add* fills in the model form. |
| **Users** | Add users (admin or user), disable them, reset their password, set their total token limit and per-model rights and limits, reset their usage. |
| **Requests** | The latest requests, by user, model, or only problems. |
| **Settings** | The manager's name, time zone (when days/weeks/months start), the default total limit per user, the sign-in length, where Discovery looks, reset everyone's usage, and the change log. |

### Limits

A user's tokens (prompt + reply) are limited twice:

* **per model**: the user's own limit on that model (Users → the user → Models → *Own rules*),
  else the model's default limit per user (Models → the model → Access & limits);
* **in total** across models: the user's limit, else the default in Settings.

Each limit counts per `day`, `week` (from Monday), `month`, or `none` (only an admin reset starts
it over), in the manager's time zone. A request is checked before it is sent; a user past a limit
gets `429` with code `token_limit` and the reset time. Dcode shows it at once (it doesn't wait and
retry). Resetting a user (or everyone) starts their count from now; the history stays.

Access: a model is open to **everyone** or to **chosen users** only. A user's *Own rules* can also
deny a model that is open to everyone. Admins may use every model.

### Guards

Per model, checked on every request (Models → the model → Guard):

| Guard | Effect |
|---|---|
| Refuse requests that match | Regexes on the user's messages; the request is refused with the pattern's name (`400 guard_blocked`), and logged. |
| Mask secrets | API keys, tokens, passwords, private keys and connection-string passwords become `[masked]` before the model sees them. |
| Mask in answers | Regexes masked in replies, streamed or not (a match split across streamed chunks is caught). |
| Requests per minute | Per user, per model (`429 rate_limit` with `Retry-After`). |
| Largest request | In characters, before it reaches the model. |
| Policy prompt | Put in front of every conversation. |
| Guard model | Another model on the manager reads the user's last message first; "unsafe" (Llama Guard) or UNSAFE refuses it. |

### Reasoning and model families

Clients send one `reasoning_effort` (`none`/`low`/`medium`/`high`). The manager sends it to each
model's server in that server's own form (vLLM chat-template switch, OpenRouter `reasoning`,
`reasoning_effort`, …), using Dcode's family detection on the name the server uses. A field a
server refuses, or a model that can't stop thinking, is learned per model. `/v1/models` tells
clients each model's family and context length, so a model called `coder` still gets the right
handling in Dcode. Set the family by hand for a fine-tune whose name says nothing.

### Kubernetes

Starting/stopping a model scales its linked deployment (0 or 1 replica). Changing its context
length can also patch the deployment's `--max-model-len` (the server restarts). Both ask first and
say what happens. The manager needs the RBAC in `deploy/manager/rbac.yaml` in each namespace it
manages; without a cluster (or the RBAC), everything else works and these buttons say why not.

## Running it

```bash
dcode manager create-admin admin            # once; asks for the password
dcode manager serve --port 8080 --data /var/lib/dcode-manager
```

Options: `--host` (default `0.0.0.0`), `--public-url` (the address people use, when behind a
proxy; or `DCODE_MANAGER_URL`), `--cert`/`--key` for TLS. The first admin can come from the
environment on first start: `DCODE_MANAGER_ADMIN` and `DCODE_MANAGER_ADMIN_PASSWORD`.
`dcode manager set-password <user>` resets a password from the server.

On Kubernetes: `deploy/manager` (see its README).

## Security

* Passwords: scrypt. Web sessions and API keys are random tokens stored only as SHA-256 hashes;
  a key is shown once. Eight failed sign-ins for a username from one address pause sign-in for it
  for five minutes.
* Web sign-in is an HttpOnly, SameSite=Strict cookie; every change also needs the
  `X-Dcode-Manager` header, which another site's page can't send. Strict CSP, no frames.
* An API key reaches the gateway and the user's own read-only `/api/me` routes, nothing else.
* A model's server key is stored in the database (on the volume) and never returned by the API.
* The last admin can't be demoted, disabled or deleted.
* Discovery and deployment changes use only the RBAC you grant, and only on an admin's click.

## API

Gateway (API key): `GET /v1/models`, `GET /v1/models/<id>`, `POST /v1/chat/completions`,
`/v1/completions`, `/v1/embeddings` and any other `POST /v1/…` route with a `model` field (rerank,
score).

Sign-in: `POST /api/login`, `POST /api/logout`, `POST /api/device/login` (Dcode: username,
password, device → API key, base URL, models), `GET /api/info`.

Signed-in user: `GET /api/me`, `/api/me/usage`, `/api/me/models`, `/api/me/keys`,
`POST /api/me/keys`, `DELETE /api/me/keys/<id>`, `POST /api/me/password`.

Admin: `/api/admin/settings`, `/api/admin/users[/<id>[/access|/usage|/reset-usage]]`,
`/api/admin/reset-usage`, `/api/admin/models[/<id>[/test|/deployment|/scale|/max-model-len]]`,
`/api/admin/discovery`, `/api/admin/monitoring[/live]`, `/api/admin/requests`,
`/api/admin/clients`, `/api/admin/audit`.
