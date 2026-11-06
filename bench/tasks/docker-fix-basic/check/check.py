import re

from benchcheck import docker_build_if_possible, finish, ok, read

BASE = "python:3.12-slim-bookworm"
lines = [ln.strip() for ln in read("Dockerfile").splitlines() if ln.strip() and not ln.strip().startswith("#")]
ok(lines and lines[0] == f"FROM {BASE}", "keeps the base image")
ok(any(re.match(r"COPY\s+(\./)?app\.py\s", ln) or re.match(r"COPY\s+\.\s", ln) for ln in lines), "copies app.py")
ok(not any("server.py" in ln for ln in lines), "no server.py left")
ok(any(re.fullmatch(r"EXPOSE\s+8080(/tcp)?", ln) for ln in lines), "exposes 8080")
ok(not any(re.match(r"EXPOSE\s+5000", ln) for ln in lines), "not 5000")
cmd = [ln for ln in lines if ln.startswith(("CMD", "ENTRYPOINT"))]
ok(cmd and "app.py" in cmd[-1], "starts app.py")
workdir = [ln.split(None, 1)[1] for ln in lines if ln.startswith("WORKDIR")]
copied_abs = any(re.match(r"COPY\s+(\./)?app\.py\s+/app(/app\.py|/)?$", ln) for ln in lines)
absolute = cmd and "/app/app.py" in cmd[-1]
ok(workdir == ["/app"] or absolute, "runs where the file is")
ok(workdir == ["/app"] or copied_abs, "the file lands in /app")
built = docker_build_if_possible(BASE)
if built is not None:
    ok(built, "a real build works")
finish()
