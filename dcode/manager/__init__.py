"""Dcode Manager: one place for a team's models, users, limits and usage.

It is a model gateway (OpenAI-compatible ``/v1``) in front of every model server, plus a web UI:

* admins add models (by hand or from cluster discovery), turn them on and off, set their context
  length, guards, and who may use them with how many tokens per day/week/month;
* users sign in (Dcode's setup does it for them), get an API key, and see their usage and when it resets;
* every request is counted, so admins see usage and each model's latency and throughput.

Standard library only (air-gapped clusters): ``http.server`` + ``sqlite3``. Start it with
``dcode manager serve``; see ``docs/manager.md``.
"""
