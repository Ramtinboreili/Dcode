from benchcheck import finish, nginx_find, nginx_parse, nginx_test_if_possible, ok, read

try:
    tree = nginx_parse(read("nginx.conf"))
except ValueError as exc:
    tree = []
    ok(False, f"the config parses: {exc}")
ok(tree, "the config has content")
upstreams = {args[0]: children for _, args, children in nginx_find(tree, "upstream") if args and children is not None}
servers = [args for upstream in upstreams.values() for name, args, _ in upstream if name == "server"]
ok(["127.0.0.1:8080"] in servers, "the upstream's server is 127.0.0.1:8080")
ok(any(args[0] == "/api/" or args[0] == "/api" for _, args, _ in nginx_find(tree, "location")), "still has the /api location")
locations = {args[-1]: children for _, args, children in nginx_find(tree, "location")}
passes = [args for name, args, _ in (locations.get("/api/") or locations.get("/api") or []) if name == "proxy_pass"]
ok(len(passes) == 1 and len(passes[0]) == 1, "one proxy_pass")
if passes:
    target = passes[0][0].replace("http://", "").rstrip("/")
    ok(target in upstreams or target == "127.0.0.1:8080", "proxy_pass reaches the upstream")
root = [args for name, args, _ in (locations.get("/") or []) if name == "root"]
ok(root == [["/var/www/site"]], "the site's root is /var/www/site")
ok(any(name == "index" and args == ["index.html"] for name, args, _ in (locations.get("/") or [])), "the index is index.html")
ok(nginx_find(tree, "listen") and nginx_find(tree, "listen")[0][1] == ["8081"], "still listens on 8081")
tested = nginx_test_if_possible("nginx.conf")
if tested is not None:
    ok(tested, "nginx -t accepts it")
finish()
