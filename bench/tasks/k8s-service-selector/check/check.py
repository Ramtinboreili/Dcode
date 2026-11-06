from benchcheck import dig, finish, load_yaml, ok

dep, svc = load_yaml("k8s/deployment.yaml"), load_yaml("k8s/service.yaml")
labels = dig(dep, "spec", "template", "metadata", "labels", default={})
selector = dig(svc, "spec", "selector", default={})
ok(labels == {"app": "web", "tier": "frontend"}, "the pods' labels weren't changed")
ok(dig(dep, "spec", "selector", "matchLabels") == labels, "the deployment still selects its pods")
ok(selector and all(labels.get(k) == v for k, v in selector.items()), "the service selector matches the pods")
port = dig(svc, "spec", "ports", 0, default={})
ok(port.get("port") == 80, "the service still listens on 80")
ok(port.get("targetPort") in (8080, "http"), "targetPort reaches the container's port")
ok(dig(dep, "spec", "template", "spec", "containers", 0, "ports", 0, "containerPort") == 8080, "the container port wasn't changed")
finish()
