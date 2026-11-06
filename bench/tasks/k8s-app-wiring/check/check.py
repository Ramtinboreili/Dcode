from benchcheck import dig, finish, load_yaml, ok

dep = load_yaml("k8s/deployment.yaml")
svc = load_yaml("k8s/service.yaml")
ing = load_yaml("k8s/ingress.yaml")
net = load_yaml("k8s/networkpolicy.yaml")
cfg = load_yaml("k8s/configmap.yaml")
sec = load_yaml("k8s/secret.yaml")
pvc = load_yaml("k8s/pvc.yaml")

ok(dig(dep, "metadata", "name") == "blog" and dig(svc, "metadata", "name") == "blog", "deployment and service keep their names")
ok(dig(dep, "metadata", "namespace") == "blog", "the deployment is in the blog namespace")
pods = dig(dep, "spec", "template", "metadata", "labels", default={})
pod = dig(dep, "spec", "template", "spec", default={})
app = dig(pod, "containers", 0, default={})
selector = dig(svc, "spec", "selector", default={})
ok(selector and all(pods.get(k) == v for k, v in selector.items()), "the service selects the pods")
ok(all(pods.get(k) == v for k, v in dig(dep, "spec", "selector", "matchLabels", default={"x": "y"}).items()), "the deployment selects its pods")
ports = {p.get("name"): p.get("containerPort") for p in app.get("ports", [])}
target = dig(svc, "spec", "ports", 0, "targetPort")
ok(target in ports or target in ports.values(), "the service's targetPort exists on the container")
ok(3000 in ports.values(), "the container port is still 3000")

env = {e["name"]: e.get("valueFrom", e) for e in app.get("env", [])}
cfg_keys = dig(cfg, "data", default={})
sec_keys = {**dig(sec, "stringData", default={}), **(dig(sec, "data", default={}) or {})}
for name, want_key in (("BLOG_TITLE", "My Blog"), ("DB_HOST", "postgres.blog.svc.cluster.local")):
    ref = env.get(name, {}).get("configMapKeyRef", {})
    ok(ref.get("name") == cfg["metadata"]["name"], f"{name} names the real ConfigMap")
    ok(cfg_keys.get(ref.get("key")) == want_key, f"{name} reads the right key")
ref = env.get("DB_PASSWORD", {}).get("secretKeyRef", {})
ok(ref.get("name") == sec["metadata"]["name"], "DB_PASSWORD names the real Secret")
ok(ref.get("key") in sec_keys, "DB_PASSWORD reads a key the Secret has")

volumes = {v["name"]: v for v in pod.get("volumes", [])}
mounts = {m["name"]: m["mountPath"] for m in app.get("volumeMounts", [])}
ok(dig(volumes, "uploads", "persistentVolumeClaim", "claimName") == pvc["metadata"]["name"], "the volume names the real PVC")
ok(mounts.get("uploads") == "/data", "uploads are mounted at /data")

backend = dig(ing, "spec", "rules", 0, "http", "paths", 0, "backend", "service", default={})
ok(backend.get("name") == svc["metadata"]["name"], "the ingress points at the service")
ok(dig(backend, "port", "number") == dig(svc, "spec", "ports", 0, "port") or dig(backend, "port", "name") == dig(svc, "spec", "ports", 0, "name"), "and at a port it has")

match = dig(net, "spec", "podSelector", "matchLabels", default={"x": "y"})
ok(all(pods.get(k) == v for k, v in match.items()), "the network policy selects the pods")
allowed = dig(net, "spec", "ingress", 0, "ports", 0, "port")
ok(allowed in (3000, "web"), "the network policy lets the app's port in")
finish()
