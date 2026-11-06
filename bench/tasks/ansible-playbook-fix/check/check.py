from benchcheck import dig, finish, load_yaml, ok, read, run, tool_enabled

plays = load_yaml("site.yml")
play = plays[0] if plays else {}
ok(play.get("hosts") == "web", "targets the group that's in the inventory")
ok(play.get("become") in (True, "yes", "true"), "still becomes root")
tasks = {t.get("name"): t for t in play.get("tasks", [])}
handlers = play.get("handlers", [])
names = [h.get("name") for h in handlers]
listens = [h.get("listen") for h in handlers]
notify = dig(tasks, "write config", "notify")
ok(notify in names or notify in listens or notify == ["restart nginx"], "the notify matches a handler")
states = [dig(h, "service", "state") or dig(h, "ansible.builtin.service", "state") for h in handlers]
ok("restarted" in states, "the handler uses a real state: restarted")
when = dig(tasks, "start nginx", "when")
ok(isinstance(when, str) and "{{" not in when, "when has no jinja braces")
ok(when and "ansible_os_family" in when, "the condition is still there")
ok(dig(tasks, "install nginx", "apt", "name") == "nginx", "the install task is still there")
ok(dig(play, "vars", "nginx_port") == 8080, "the port var is still there")
ok("[web]" in read("inventory.ini"), "the inventory wasn't renamed instead")
if tool_enabled("ansible-playbook"):
    result = run("ansible-playbook", "--syntax-check", "-i", "inventory.ini", "site.yml", timeout=120)
    ok(result.returncode == 0, "ansible --syntax-check: " + (result.stderr or result.stdout)[-300:])
else:
    print("SKIP ansible --syntax-check: ansible isn't available")
finish()
