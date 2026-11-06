"""With no experiment section and no trace, Dcode sends the same bytes as before the instrumentation existed.

``tests/golden/requests.json.gz`` holds every request body of a scripted session, recorded on commit 98c130b by
``tests/golden/scenario.py``, which also says how to regenerate it after a deliberate change to a prompt or schema.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "golden"))
import scenario  # noqa: E402

from dcode import experiment  # noqa: E402


def _first_difference(old: str, new: str) -> str:
    cut = next((i for i, (a, b) in enumerate(zip(old, new, strict=False)) if a != b), min(len(old), len(new)))
    return f"at char {cut}:\n  was {old[max(0, cut - 80):cut + 120]!r}\n  now {new[max(0, cut - 80):cut + 120]!r}"


def _same(golden: list[dict], fresh: list[dict]) -> None:
    assert len(fresh) == len(golden), f"{len(golden)} requests were recorded, {len(fresh)} sent"
    for index, (old, new) in enumerate(zip(golden, fresh, strict=True)):
        assert old["path"] == new["path"]
        assert old["body"] == new["body"], f"request {index} differs {_first_difference(old['body'], new['body'])}"


@pytest.mark.parametrize("name", [n for n in scenario.SCENARIOS if n != "long-writes"])
def test_requests_are_byte_identical_to_the_recording(name):
    _same(scenario.load_golden()[name], scenario.record(name))


def test_spelling_out_every_default_changes_nothing():
    name = "compaction"
    _same(scenario.load_golden()[name], scenario.record(name, experiment=experiment.defaults()))


def test_tracing_changes_nothing_on_the_wire(tmp_path):
    name = "session-stream"
    settings = {"path": str(tmp_path / "trace.jsonl"), "snapshots": True}
    _same(scenario.load_golden()[name], scenario.record(name, trace=settings))
    rows = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    sends = [row["http_sends"] for row in rows if row["type"] == "llm_call"]
    assert sum(sends) == len(scenario.load_golden()[name])  # every request on the wire is in the trace, the SDK's own retry too
    assert max(sends) == 2


def test_the_recording_is_what_it_claims_to_be():
    """A guard against testing nothing: the scenarios really reach the paths they are there for."""
    golden = scenario.load_golden()
    bodies = [r["body"] for rows in golden.values() for r in rows]
    assert sum(len(rows) for rows in golden.values()) > 100
    assert any("[Session summary of earlier conversation]" in b for b in bodies)
    assert any("Summarize this coding-session" in b for b in bodies)
    assert any("from an earlier turn" in b for b in bodies)            # earlier-turn elision
    assert any("to save context; re-run" in b for b in bodies)         # compaction stage 1
    assert any("Memories relevant to the current task" in b for b in bodies)
    assert any("exploration sub-agent" in b for b in bodies)
    assert any('"model": "big-32b"' in b or '"model":"big-32b"' in b for b in bodies)
    assert len({json.loads(b).get("stream", False) for b in bodies}) == 2
