import re

from benchcheck import dig, finish, load_yaml, ok, read, run, tool_enabled

values = load_yaml("chart/values.yaml") or {}
ok(dig(values, "replicaCount") == 2, "replicaCount is 2")
ok(dig(values, "image", "repository") == "registry.local/shop", "image.repository")
ok(str(dig(values, "image", "tag")) == "2.1", "image.tag")
ok(isinstance(dig(values, "image", "tag"), str), "the tag is quoted, or 2.10 would become 2.1")
ok(dig(values, "service", "port") == 80, "service.port")
ok(dig(values, "service", "targetPort") == 8080, "service.targetPort")
ok(isinstance(dig(values, "resources", "limits"), dict), "resources.limits exists, or the template's nil check fails")
for name in ("deployment", "service"):
    text = read(f"chart/templates/{name}.yaml")
    for ref in re.findall(r"\.Values\.([A-Za-z0-9_.]+)", text):
        if not ref.endswith(".memory"):
            ok(dig(values, *ref.split(".")) is not None, f"{name} needs .Values.{ref}")
    ok(".Values.replicaCount" in read("chart/templates/deployment.yaml"), "templates were left alone")
if tool_enabled("helm"):
    result = run("helm", "template", "rel", "chart")
    ok(result.returncode == 0, "helm template renders: " + result.stderr[-300:])
    ok('image: "registry.local/shop:2.1"' in result.stdout, "the image renders right")
    ok("replicas: 2" in result.stdout, "the replicas render right")
else:
    print("SKIP helm template: helm isn't available")
finish()
