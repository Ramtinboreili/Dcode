# Conventions

- One folder per service: `k8s/<name>/deployment.yaml`, `service.yaml`, and `ingress.yaml` if the service has a `host`.
- Everything goes in namespace `shop`.
- Labels on every object and on the pods: `app.kubernetes.io/name: <name>`, `app.kubernetes.io/part-of: shop`. Selectors use only the name label.
- Containers: readiness on `/ready`, liveness on `/healthz`, both http on the service's port. The liveness probe needs an initialDelaySeconds of at least 15.
- Requests come from `services.json`; limits are twice the requests. Don't invent other values.
- `securityContext` on the pod: `runAsNonRoot: true`. On the container: `allowPrivilegeEscalation: false`.
- Services are ClusterIP on port 80, with a named targetPort `http`.
- Ingress: `ingressClassName: traefik`, path `/` Prefix, tls with secret `<name>-tls`.
