# Dcode Manager on Kubernetes

What it is and how to use it: [docs/manager.md](../../docs/manager.md).

```bash
make docker                                   # builds dcode:<version> (packaging/Dockerfile)
docker tag dcode:0.8.0 <registry>/dcode:0.8.0 && docker push <registry>/dcode:0.8.0
# edit kustomization.yaml (image), secret.yaml (admin password), rbac.yaml (model namespace)
kubectl apply -k deploy/manager
kubectl -n dcode rollout status deploy/dcode-manager
```

Then open `http://<any node>:31010`, sign in as the admin, and add models (Discovery finds the
model servers in the namespaces from Settings, `vllm` by default). People connect with
`dcode login http://<node>:31010`.

| File | What |
|---|---|
| `namespace.yaml` | Namespace `dcode`. |
| `secret.yaml` | The first admin (only used while the database has no user). |
| `rbac.yaml` | ServiceAccount; a Role + RoleBinding **in the model namespace**: read services, pods, deployments; patch deployments and `deployments/scale` (start/stop, `--max-model-len`). Copy the Role and RoleBinding for each extra namespace. |
| `pvc.yaml` | 2 GiB for the SQLite database (default storage class). |
| `deployment.yaml` | One replica (`Recreate`: never two pods on one SQLite file), non-root, read-only root filesystem with a writable `/tmp` (the binary unpacks there), health probes. |
| `service.yaml` | ClusterIP `dcode-manager:8080` and NodePort `31010`. |

Inside the cluster, model servers are reached by their service names
(`http://vllm-gemma4.vllm.svc:8000/v1`). Behind an Ingress with TLS, set `DCODE_MANAGER_URL` in
the deployment to the public address so Dcode is given the right gateway URL.

**Models folder (optional).** To list model weights in Discovery, mount the weights folder
read-only at `/models` (the commented `hostPath` in `deployment.yaml`), pin the pod to that node
(a `nodeSelector`, and the GPU taint's toleration on a GPU node), and set Settings → Models folder
to `/models`.

**Backups.** Everything is in `/data/manager.db`. Copy it with its `-wal` file while it runs
(`kubectl -n dcode cp <pod>:/data ./manager-backup`), or scale the deployment to 0 first for a
clean copy.
