import re

from benchcheck import dig, finish, load_yaml, ok, read, run, tool_enabled

group = load_yaml("group_vars/app.yml")
ok(group == {"app_version": "1.8.2", "app_db_password": "s3cret", "app_user": "appsvc"}, "group_vars weren't changed")
defaults = load_yaml("roles/app/defaults/main.yml") or {}
ok(defaults.get("app_port") == 9000, "app_port defaults to 9000")
known = set(defaults) | set(group)
tasks_text = read("roles/app/tasks/main.yml")
template = read("roles/app/templates/app.conf.j2")
for label, text in (("tasks", tasks_text), ("template", template)):
    used = set(re.findall(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)", text))
    ok(used and used <= known, f"{label} only use variables that exist (extra: {sorted(used - known)})")
for key in ("port", "user", "db_password"):
    ok(re.search(rf"^{key}\s*=\s*\{{\{{", template, re.M), f"the config still sets {key}")
ok("app_port" in template and "app_user" in template and "app_db_password" in template, "the template reads the right vars")
tasks = load_yaml("roles/app/tasks/main.yml") or []


def find(fragment):
    return next((t for t in tasks if fragment in str(t.get("name", ""))), {})


download = find("download")
creates = dig(download, "args", "creates") or dig(download, "command", "creates") or download.get("creates")
fetched = dig(download, "get_url", "dest") or dig(download, "ansible.builtin.get_url", "dest")
ok((creates and "app_version" in creates) or fetched, "the download is skipped when the file is there")
if not fetched:
    ok("app_version" in str(download.get("command")), "the download uses app_version")
password = find("password")
ok(password.get("no_log") is True or password.get("no_log") == "true", "the password task has no_log")
service = find("start")
ok(dig(service, "service", "enabled") in (True, "yes", "true") or dig(service, "ansible.builtin.service", "enabled") in (True, "yes", "true"), "the service is enabled on boot")
ok(dig(service, "service", "state") == "started" or dig(service, "ansible.builtin.service", "state") == "started", "and started")
handlers = load_yaml("roles/app/handlers/main.yml") or []
config = find("config")
ok(config.get("notify") in [h.get("name") for h in handlers] + [[h.get("name")] for h in handlers], "the config's notify matches the handler")
mode = str(dig(config, "template", "mode") or dig(config, "ansible.builtin.template", "mode") or "")
ok(mode and mode[-1] == "0", "a config holding a password isn't world readable")
if tool_enabled("ansible-playbook"):
    result = run("ansible-playbook", "--syntax-check", "-i", "localhost,", "deploy.yml", timeout=120)
    ok(result.returncode == 0, "ansible --syntax-check: " + (result.stderr or result.stdout)[-300:])
else:
    print("SKIP ansible --syntax-check: ansible isn't available")
finish()
