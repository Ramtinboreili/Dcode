"""Find model servers on the cluster and model weights on disk; start, stop and resize deployments.

Kubernetes is reached through its REST API with the pod's service account (in-cluster), or a URL
such as ``kubectl proxy`` (``http://127.0.0.1:8001``) when the manager runs elsewhere. Needs, in the
watched namespaces: get/list on services, pods and deployments; patch on deployments and
deployments/scale (only to start/stop a model or change ``--max-model-len``). See ``deploy/manager``.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
PROBE_TIMEOUT = 2.5
SERVER_IMAGES = re.compile(r"vllm|sglang|text-generation-inference|tgi|llama\.?cpp|ollama|lmdeploy|triton|tabby|"
                           r"localai|openai", re.IGNORECASE)
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt")


class KubeError(Exception):
    pass


class Kube:
    def __init__(self, api: str = "") -> None:
        self.token = ""
        self.context: ssl.SSLContext | None = None
        if api:
            self.base = api.rstrip("/")
        else:
            host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            if not host or not (SA_DIR / "token").exists():
                raise KubeError("Not running in a cluster: set Settings → Kubernetes API (e.g. a `kubectl proxy` URL).")
            self.base = f"https://{host}:{port}"
            self.token = (SA_DIR / "token").read_text().strip()
            self.context = ssl.create_default_context(cafile=str(SA_DIR / "ca.crt"))

    def request(self, method: str, path: str, body: Any = None,
                content_type: str = "application/merge-patch+json") -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", content_type)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=15, context=self.context) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode(errors="replace")
            try:
                detail = json.loads(detail).get("message", detail)
            except ValueError:
                pass
            raise KubeError(f"Kubernetes said {exc.code}: {detail}") from exc
        except OSError as exc:
            raise KubeError(f"Kubernetes API not reachable at {self.base}: {exc}") from exc

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path)


# --- reading a model server's deployment -------------------------------------------------------
def _flag(args: list[str], *names: str) -> str:
    for name in names:  # in the order given: the first name is the most specific
        for index, arg in enumerate(args):
            if arg == name and index + 1 < len(args):
                return args[index + 1]
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return ""


def server_args(container: dict[str, Any]) -> dict[str, Any]:
    """What a vLLM-style command line says about the model it serves."""
    words = [str(w) for w in (container.get("command") or []) + (container.get("args") or [])]
    if len(words) <= 3 and any(" " in w for w in words):  # ["sh", "-c", "vllm serve /m --port 8010 …"]
        words = [part for w in words for part in w.split()]
    model = _flag(words, "--model")
    if not model and "serve" in words:
        after = words[words.index("serve") + 1:]
        model = after[0] if after and not after[0].startswith("-") else ""
    length = _flag(words, "--max-model-len", "--max_model_len", "--context-length", "--ctx-size")
    return {"model_path": model, "served_name": _flag(words, "--served-model-name").split(",")[0] if
            _flag(words, "--served-model-name") else "",
            "max_model_len": int(length) if length.isdigit() else None,
            "port": _flag(words, "--port"),
            "gpu_memory_utilization": _flag(words, "--gpu-memory-utilization")}


def _selected(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def probe(url: str, api_key: str = "") -> tuple[list[dict[str, Any]], str]:
    """(models a server lists at <url>/models, error)."""
    request = urllib.request.Request(url.rstrip("/") + "/models", headers={"Accept": "application/json"})
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            payload = json.load(response)
    except Exception as exc:  # noqa: BLE001 - every failure is just "not reachable"
        return [], str(getattr(exc, "reason", exc))[:200]
    models = []
    for item in payload.get("data") or []:
        if isinstance(item, dict) and item.get("id"):
            models.append({"id": item["id"], "max_model_len": item.get("max_model_len") or item.get("context_length")})
    return models, ""


def discover(kube: Kube, namespaces: list[str], known: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every model server in ``namespaces``: its deployment, service URL, what it serves, and whether it's added."""
    found = []
    for namespace in namespaces:
        deployments = kube.get(f"/apis/apps/v1/namespaces/{namespace}/deployments").get("items") or []
        services = kube.get(f"/api/v1/namespaces/{namespace}/services").get("items") or []
        for deployment in deployments:
            spec = deployment.get("spec") or {}
            template = (spec.get("template") or {})
            labels = (template.get("metadata") or {}).get("labels") or {}
            containers = (template.get("spec") or {}).get("containers") or []
            if not containers:
                continue
            container = next((c for c in containers if SERVER_IMAGES.search(c.get("image", ""))), containers[0])
            args = server_args(container)
            looks_like_server = SERVER_IMAGES.search(container.get("image", "")) or args["model_path"]
            service = next((s for s in services if (s.get("spec") or {}).get("type") != "NodePort"
                            and _selected((s.get("spec") or {}).get("selector") or {}, labels)), None) or next(
                (s for s in services if _selected((s.get("spec") or {}).get("selector") or {}, labels)), None)
            if not looks_like_server and service is None:
                continue
            name = deployment["metadata"]["name"]
            url = ""
            if service is not None:
                ports = (service.get("spec") or {}).get("ports") or []
                port = ports[0].get("port") if ports else 80
                url = f"http://{service['metadata']['name']}.{namespace}.svc:{port}/v1"
            replicas, ready = spec.get("replicas", 1), (deployment.get("status") or {}).get("readyReplicas") or 0
            served, error = (probe(url) if url and ready else ([], "scaled to 0" if not replicas else "not ready"))
            volumes = [(v.get("hostPath") or {}).get("path") for v in (template.get("spec") or {}).get("volumes") or []]
            found.append({
                "namespace": namespace, "deployment": name, "container": container.get("name", ""),
                "image": container.get("image", ""), "replicas": replicas, "ready": ready,
                "service": service["metadata"]["name"] if service else "", "url": url,
                "served": served, "error": error, "args": args,
                "host_paths": [p for p in volumes if p],
                "added": [m["id"] for m in known if (m.get("deployment") or {}).get("name") == name
                          and (m.get("deployment") or {}).get("namespace") == namespace]
                or [m["id"] for m in known if url and m.get("upstream_url", "").rstrip("/") == url],
            })
    return found


# --- changing a deployment (asked for in the UI, never automatic) ---------------------------------------
def scale(kube: Kube, namespace: str, name: str, replicas: int) -> dict[str, Any]:
    return kube.request("PATCH", f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale",
                        {"spec": {"replicas": int(replicas)}})


def deployment_state(kube: Kube, namespace: str, name: str) -> dict[str, Any]:
    deployment = kube.get(f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}")
    status = deployment.get("status") or {}
    containers = ((deployment.get("spec") or {}).get("template") or {}).get("spec", {}).get("containers") or []
    container = next((c for c in containers if SERVER_IMAGES.search(c.get("image", ""))), containers[0] if containers else {})
    return {"replicas": (deployment.get("spec") or {}).get("replicas", 0), "ready": status.get("readyReplicas") or 0,
            "updated": status.get("updatedReplicas") or 0, "args": server_args(container)}


def with_max_model_len(words: list[str], value: int) -> list[str]:
    """The same command line with --max-model-len set to ``value`` (replaced, or added at the end)."""
    out, index, done = [], 0, False
    while index < len(words):
        word = words[index]
        if word in ("--max-model-len", "--max_model_len") and index + 1 < len(words):
            out += [word, str(value)]
            index += 2
            done = True
            continue
        if word.startswith(("--max-model-len=", "--max_model_len=")):
            out.append(f"{word.split('=', 1)[0]}={value}")
            done = True
        elif " --max-model-len" in word or " --max_model_len" in word:  # inside "sh -c '…'"
            out.append(re.sub(r"(--max[-_]model[-_]len[= ])\d+", rf"\g<1>{value}", word))
            done = True
        else:
            out.append(word)
        index += 1
    if not done:
        if len(out) == 3 and out[1] == "-c":
            out[2] = out[2].rstrip() + f" --max-model-len {value}"
        else:
            out += ["--max-model-len", str(value)]
    return out


def set_max_model_len(kube: Kube, namespace: str, name: str, container_name: str, value: int) -> list[str]:
    """Patch the deployment's command line; Kubernetes then restarts the model server with it."""
    deployment = kube.get(f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}")
    containers = deployment["spec"]["template"]["spec"]["containers"]
    container = next((c for c in containers if c.get("name") == container_name), containers[0])
    field = "args" if container.get("args") else "command"
    words = with_max_model_len([str(w) for w in container.get(field) or []], value)
    kube.request("PATCH", f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
                 {"spec": {"template": {"spec": {"containers": [{"name": container["name"], field: words}]}}}},
                 content_type="application/strategic-merge-patch+json")
    return words


# --- weights on disk ------------------------------------------------------------------------------------
def scan_weights(root: str, deployments: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Model folders (a config.json with weights) and .gguf files under ``root``, two levels deep."""
    base = Path(root).expanduser()
    if not root or not base.is_dir():
        return []
    served_names = set()
    for item in deployments or []:
        for path in [item["args"].get("model_path", "")] + item.get("host_paths", []):
            if path:
                served_names.add(Path(path).name.lower())
    found = []
    candidates = [base] + [p for p in sorted(base.iterdir()) if p.is_dir()]
    candidates += [q for p in candidates[1:] for q in sorted(p.iterdir()) if q.is_dir()]
    for folder in candidates:
        try:
            files = [f for f in folder.iterdir() if f.is_file()]
        except OSError:
            continue
        for gguf in (f for f in files if f.suffix == ".gguf"):
            found.append({"name": gguf.stem, "path": str(gguf), "format": "gguf", "size": gguf.stat().st_size,
                          "served": gguf.stem.lower() in served_names or folder.name.lower() in served_names})
        config = folder / "config.json"
        weights = [f for f in files if f.suffix in WEIGHT_SUFFIXES and f.suffix != ".gguf"]
        if config.exists() and weights:
            try:
                meta = json.loads(config.read_text())
            except (OSError, ValueError):
                meta = {}
            text = meta.get("text_config") if isinstance(meta.get("text_config"), dict) else meta
            found.append({
                "name": folder.name, "path": str(folder), "format": "safetensors",
                "size": sum(f.stat().st_size for f in weights),
                "architecture": (meta.get("architectures") or [""])[0],
                "context": text.get("max_position_embeddings") or meta.get("max_position_embeddings"),
                "dtype": meta.get("torch_dtype") or text.get("torch_dtype") or "",
                "served": folder.name.lower() in served_names,
            })
    return found
