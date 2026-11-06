from benchcheck import dig, finish, load_yaml, ok

doc = load_yaml("deploy/payments.yaml")
containers = {c["name"]: c for c in dig(doc, "spec", "template", "spec", "containers", default=[])}
app = containers.get("payments", {})
side = containers.get("log-shipper", {})


def boot_allowance(probe):
    """Seconds a slow start is tolerated: an initial delay, a startupProbe, or failures before giving up."""
    delay = probe.get("initialDelaySeconds", 0)
    return delay + probe.get("periodSeconds", 10) * probe.get("failureThreshold", 3)


for name in ("readinessProbe", "livenessProbe"):
    probe = app.get(name, {})
    ok(dig(probe, "httpGet", "path") == "/healthz", f"{name} is on /healthz")
    ok(dig(probe, "httpGet", "port") in (8080, "http"), f"{name} is on the app's port")
live = app.get("livenessProbe", {})
ok(boot_allowance(live) >= 30 or "startupProbe" in app, "liveness gives the app its 30 seconds")
ok(float(str(dig(app, "resources", "limits", "cpu", default="0")).rstrip("m") or 0) in (1.0, 1000.0), "cpu limit is 1")
ok(dig(app, "resources", "limits", "memory") in ("512Mi",), "memory limit is 512Mi")
ok(dig(app, "resources", "requests", "cpu") in ("250m", 0.25, "0.25"), "cpu request is 250m")
ok(dig(app, "resources", "requests", "memory") == "256Mi", "memory request is 256Mi")
ok(side == {"name": "log-shipper", "image": "registry.local/fluent-bit:3.0",
            "volumeMounts": [{"name": "logs", "mountPath": "/var/log/app"}]}, "the sidecar is untouched")
ok(app.get("image") == "registry.local/payments:2.8.1", "image unchanged")
ok(dig(doc, "spec", "replicas") == 3, "replicas unchanged")
ok(dig(doc, "metadata", "namespace") == "prod", "namespace unchanged")
finish()
