from benchcheck import dig, finish, load_yaml, ok

static = load_yaml("traefik/traefik.yaml")
http = dig(load_yaml("traefik/dynamic.yaml"), "http", default={})
routers = http.get("routers", {})
middlewares = http.get("middlewares", {})
services = http.get("services", {})
entry = set(static["entryPoints"])
resolvers = set(static["certificatesResolvers"])
HOST = "Host(`grafana.example.internal`)"

secure = [r for r in routers.values() if dig(r, "tls") is not None and HOST in str(r.get("rule"))]
ok(len(secure) == 1, "one TLS router for the host, with the rule in backticks")
router = secure[0] if secure else {}
ok(router.get("entryPoints") == ["websecure"], "https only, on an entry point that exists")
ok(set(router.get("entryPoints", [])) <= entry, "entry points exist")
ok(dig(router, "tls", "certResolver") in resolvers, "the resolver exists")
ok(dig(router, "tls", "certResolver") == "letsencrypt", "it is letsencrypt")
ok(router.get("service") in services, "the router's service exists")
used = router.get("middlewares", [])
ok(any(dig(middlewares, m, "basicAuth") for m in used), "behind the auth middleware")
ok(set(used) <= set(middlewares), "every middleware named exists")
url = dig(services, router.get("service"), "loadBalancer", "servers", 0, "url")
ok(url == "http://10.0.0.5:3000", "the service's url has its scheme")
plain = [r for r in routers.values() if "web" in (r.get("entryPoints") or []) and HOST in str(r.get("rule"))]
ok(plain, "a router catches plain http on the host")
redirects = [m for r in plain for m in r.get("middlewares", [])]
ok(any(dig(middlewares, m, "redirectScheme", "scheme") == "https" for m in redirects), "and redirects it to https")
ok(all(r.get("service") in services or str(r.get("service", "")).endswith("@internal") for r in routers.values()), "every router's service exists")
ok(all(set(r.get("middlewares", [])) <= set(middlewares) for r in routers.values()), "every router's middlewares exist")
finish()
