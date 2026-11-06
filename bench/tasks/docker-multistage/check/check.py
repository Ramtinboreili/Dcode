# Static only: the real build installs packages, and a check must stay offline.
import re

from benchcheck import exists, finish, ok, read

BASE = "python:3.12-slim-bookworm"
text = re.sub(r"\\\n\s*", " ", read("Dockerfile"))
lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
stages, current = [], None
for line in lines:
    if line.upper().startswith("FROM "):
        current = {"from": line, "lines": []}
        stages.append(current)
    elif current is not None:
        current["lines"].append(line)
ok(len(stages) >= 2, "at least two stages")
if stages:
    ok(re.fullmatch(rf"FROM\s+{re.escape(BASE)}\s+AS\s+builder", stages[0]["from"], re.I), "first stage is the pinned base, named builder")
    ok(all(BASE in s["from"] or "builder" in s["from"].split()[1:2] for s in stages), "no unpinned base anywhere")
    ok(not any("latest" in s["from"] for s in stages), "not latest")
if len(stages) >= 2:
    first, final = stages[0], stages[-1]
    ok(final["from"].split()[1] == BASE, "final stage on the pinned base")
    final_runs = " ".join(ln for ln in final["lines"] if ln.upper().startswith("RUN"))
    ok(not re.search(r"\b(gcc|make|libpq-dev|build-essential)\b", final_runs), "no build tools in the final stage")
    ok(re.search(r"\bgcc\b", " ".join(first["lines"])), "gcc in the builder")
    ok(any(re.match(r"RUN\s+make\b.*native", ln) for ln in first["lines"]), "the extension is built in the builder")
    ok(any(re.match(r"COPY\s+--from=builder\s", ln, re.I) for ln in final["lines"]), "the final stage copies from the builder")
    users = [ln.split(None, 1)[1] for ln in final["lines"] if ln.upper().startswith("USER ")]
    ok(users and users[-1] not in ("root", "0", "0:0"), "runs as a non-root user")
    index = {i: ln for i, ln in enumerate(first["lines"])}
    pip = [i for i, ln in index.items() if re.match(r"RUN\s+.*pip3?\s+install", ln)]
    reqs = [i for i, ln in index.items() if re.match(r"COPY\s+.*requirements\.txt", ln)]
    whole = [i for i, ln in index.items() if re.match(r"COPY\s+\.\s", ln)]
    ok(pip and reqs and reqs[0] < pip[0], "requirements are copied before pip runs")
    ok(pip and whole and pip[0] < whole[0], "the source is copied after pip runs")
    ok(any(re.fullmatch(r"EXPOSE\s+9000(/tcp)?", ln) for ln in final["lines"]), "still exposes 9000")
    cmd = [ln for ln in final["lines"] if ln.upper().startswith(("CMD", "ENTRYPOINT"))]
    ok(cmd and "service" in cmd[-1], "still starts the service")
    ok(any("PYTHONPATH" in ln for ln in final["lines"]) or any(re.search(r"/app", ln) for ln in final["lines"]), "the code is findable")
ok(exists(".dockerignore"), "has a .dockerignore")
ignored = [ln.strip().rstrip("/") for ln in read(".dockerignore").splitlines()]
ok(".git" in ignored, "ignores .git")
ok("tests" in ignored or "tests/" in ignored or "**/tests" in ignored, "ignores tests")
finish()
