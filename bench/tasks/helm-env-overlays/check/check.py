import copy

from benchcheck import dig, finish, load_yaml, ok, read, run, tool_enabled


def merge(base, over):
    """Helm's rule: maps merge, anything else replaces."""
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


base = load_yaml("chart/values.yaml")
ok(dig(base, "replicaCount") == 1 and dig(base, "image", "tag") == "latest", "the base file wasn't touched")
ok(dig(base, "ingress", "hosts", 0, "host") == "shop.staging.internal", "staging's host wasn't touched")
prod = merge(base, load_yaml("chart/values-prod.yaml"))
ok(dig(prod, "replicaCount") == 5, "5 replicas")
ok(dig(prod, "image", "repository") == "registry.local/shop", "the repository survives the merge")
ok(str(dig(prod, "image", "tag")) == "4.2.0", "tag 4.2.0")
ok(dig(prod, "image", "pullPolicy") == "IfNotPresent", "pullPolicy survives the merge")
hosts = dig(prod, "ingress", "hosts", default=[])
ok(len(hosts) == 1 and hosts[0].get("host") == "shop.example.com", "the only host is shop.example.com")
ok(dig(hosts, 0, "paths") == ["/"], "with the path the template loops over")
ok(dig(prod, "ingress", "enabled") is True and dig(prod, "ingress", "className") == "traefik", "the ingress keeps its other settings")
tls = dig(prod, "ingress", "tls", default=[])
ok(isinstance(tls, list) and dig(tls, 0, "secretName") == "shop-tls", "tls is a list with the secret")
ok("shop.example.com" in (dig(tls, 0, "hosts") or []), "tls names the host")
ok(dig(prod, "autoscaling", "enabled") is True, "autoscaling on")
ok(dig(prod, "autoscaling", "minReplicas") == 5 and dig(prod, "autoscaling", "maxReplicas") == 12, "from 5 to 12")
ok(dig(prod, "resources", "limits", "memory") == "2Gi", "memory limit 2Gi")
ok(dig(prod, "resources", "limits", "cpu") == "500m", "cpu limit stays the default")
ok(dig(prod, "resources", "requests", "cpu") == "100m", "requests stay the default")
over = load_yaml("chart/values-prod.yaml") or {}
ok(not set(over) - {"replicaCount", "image", "ingress", "autoscaling", "resources"}, "no keys the chart ignores")
ok("toYaml .Values.resources" in read("chart/templates/deployment.yaml"), "templates were left alone")
if tool_enabled("helm"):
    result = run("helm", "template", "rel", "chart", "-f", "chart/values-prod.yaml")
    ok(result.returncode == 0, "helm template renders: " + result.stderr[-300:])
    ok('image: "registry.local/shop:4.2.0"' in result.stdout, "the image renders right")
    ok("host: shop.example.com" in result.stdout and "shop.staging.internal" not in result.stdout, "the host renders right")
    ok("maxReplicas: 12" in result.stdout and "secretName: shop-tls" in result.stdout, "the hpa and tls render")
else:
    print("SKIP helm template: helm isn't available")
finish()
