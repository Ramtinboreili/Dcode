import re

from benchcheck import dig, finish, load_yaml, ok

flow = load_yaml(".github/workflows/ci.yml") or {}
on = flow.get("on", {})
ok(dig(on, "push", "branches") in (["main"], "main"), "runs on pushes to main")
ok("branch" not in (on.get("push") or {}), "the misspelled key is gone")
ok("pull_request" in on, "still runs on pull requests")
jobs = flow.get("jobs", {})
test, deploy = jobs.get("test", {}), jobs.get("deploy", {})
ok(set(jobs) == {"test", "deploy"}, "the jobs keep their names")
needs = deploy.get("needs")
ok(needs in ("test", ["test"]), "deploy needs test")
steps = test.get("steps", [])
setup = next((s for s in steps if "setup-python" in str(s.get("uses"))), {})
version = str(dig(setup, "with", "python-version"))
ok(re.fullmatch(r"\$\{\{\s*matrix\.python\s*\}\}", version), "setup-python uses the matrix")
ok(dig(test, "strategy", "matrix", "python") == ["3.10", "3.12"], "the matrix is unchanged, as strings")
condition = str(deploy.get("if", ""))
ok(re.search(r"github\.ref\s*==\s*'refs/heads/main'", condition) or re.search(r"github\.ref_name\s*==\s*'main'", condition), "deploy only runs on main")
run_steps = [s for s in deploy.get("steps", []) if "deploy.sh" in str(s.get("run"))]
env = {**(deploy.get("env") or {}), **(dig(run_steps, 0, "env") or {})}
ok(re.fullmatch(r"\$\{\{\s*secrets\.DEPLOY_TOKEN\s*\}\}", str(env.get("TOKEN", ""))), "the token comes from secrets")
ok(run_steps, "deploy still runs deploy.sh")
finish()
