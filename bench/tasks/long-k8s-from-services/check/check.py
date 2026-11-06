import json
import os

from benchcheck import dig, exists, finish, load_json, load_yaml, ok

services = load_json("services.json")["services"]
NAME, PART = "app.kubernetes.io/name", "app.kubernetes.io/part-of"


def double(value):
    return f"{int(value[:-1]) * 2}m" if value.endswith("m") else f"{int(value[:-2]) * 2}Mi"


ok(len(services) == 12, "services.json wasn't cut down")
for s in services:
    n = s["name"]
    labels = {NAME: n, PART: "shop"}
    if not exists(f"k8s/{n}/deployment.yaml") or not exists(f"k8s/{n}/service.yaml"):
        ok(False, f"{n}: has a deployment and a service")
        continue
    dep, svc = load_yaml(f"k8s/{n}/deployment.yaml"), load_yaml(f"k8s/{n}/service.yaml")
    ok(dep["apiVersion"] == "apps/v1" and dep["kind"] == "Deployment", f"{n}: is a Deployment")
    ok(dig(dep, "metadata", "name") == n and dig(dep, "metadata", "namespace") == "shop", f"{n}: name and namespace")
    ok(dig(dep, "metadata", "labels") == labels, f"{n}: deployment labels")
    ok(dig(dep, "spec", "replicas") == s["replicas"], f"{n}: replicas")
    ok(dig(dep, "spec", "selector", "matchLabels") == {NAME: n}, f"{n}: selector")
    ok(dig(dep, "spec", "template", "metadata", "labels") == labels, f"{n}: pod labels")
    ok(dig(dep, "spec", "template", "spec", "securityContext", "runAsNonRoot") is True, f"{n}: runAsNonRoot")
    c = dig(dep, "spec", "template", "spec", "containers", 0, default={})
    ok(c.get("image") == s["image"], f"{n}: image")
    ok(dig(c, "securityContext", "allowPrivilegeEscalation") is False, f"{n}: no privilege escalation")
    ok({p.get("name"): p.get("containerPort") for p in c.get("ports", [])} == {"http": s["port"]}, f"{n}: named port")
    ok({e["name"]: e.get("value") for e in c.get("env", [])} == s["env"], f"{n}: env")
    for key, path in (("readinessProbe", "/ready"), ("livenessProbe", "/healthz")):
        ok(dig(c, key, "httpGet", "path") == path, f"{n}: {key} path")
        ok(dig(c, key, "httpGet", "port") in ("http", s["port"]), f"{n}: {key} port")
    ok(dig(c, "livenessProbe", "initialDelaySeconds", default=0) >= 15, f"{n}: liveness delay")
    ok(dig(c, "resources", "requests") == {"cpu": s["cpu"], "memory": s["memory"]}, f"{n}: requests")
    ok(dig(c, "resources", "limits") == {"cpu": double(s["cpu"]), "memory": double(s["memory"])}, f"{n}: limits")
    ok(svc["kind"] == "Service" and dig(svc, "metadata", "namespace") == "shop", f"{n}: service kind and namespace")
    ok(dig(svc, "metadata", "labels") == labels, f"{n}: service labels")
    ok(dig(svc, "spec", "selector") == {NAME: n}, f"{n}: service selector")
    ok(dig(svc, "spec", "type", default="ClusterIP") == "ClusterIP", f"{n}: ClusterIP")
    ok(dig(svc, "spec", "ports", 0, "port") == 80 and dig(svc, "spec", "ports", 0, "targetPort") == "http", f"{n}: service port")
    path = f"k8s/{n}/ingress.yaml"
    ok(exists(path) == bool(s["host"]), f"{n}: an ingress only when it has a host")
    if s["host"] and exists(path):
        ing = load_yaml(path)
        ok(ing["apiVersion"] == "networking.k8s.io/v1" and dig(ing, "spec", "ingressClassName") == "traefik", f"{n}: ingress class")
        ok(dig(ing, "spec", "tls", 0, "secretName") == f"{n}-tls", f"{n}: tls secret")
        rule = dig(ing, "spec", "rules", 0, default={})
        ok(rule.get("host") == s["host"], f"{n}: host")
        p = dig(rule, "http", "paths", 0, default={})
        ok(p.get("path") == "/" and p.get("pathType") == "Prefix", f"{n}: path")
        ok(dig(p, "backend", "service", "name") == n and dig(p, "backend", "service", "port", "number") == 80, f"{n}: backend")
ok(sorted(os.listdir("k8s")) == sorted([s["name"] for s in services] + ["README.md"]), "one folder per service and nothing else")
finish()
