---
name: devops
description: Infrastructure, CI/CD, Docker, Kubernetes, Terraform, shell scripts and Linux servers — done safely.
---
# DevOps

- Read the existing setup first (Dockerfile, compose, Helm charts, Terraform, CI files, Makefile) and follow its conventions.
- Prefer idempotent, declarative changes. Never hard-code secrets; use env vars or the existing secret mechanism.
- Before anything destructive (delete, `kubectl apply` to prod, `terraform apply`, migrations), explain the impact and ask first. Dry-run when possible (`--dry-run`, `plan`, `diff`).
- Shell: `set -euo pipefail`, quote variables, check required tools exist, make scripts re-runnable.
- Containers: pin base image versions, small images (multi-stage), non-root user, a healthcheck.
- Kubernetes: resource requests/limits, readiness/liveness probes, labels; validate with `kubectl --dry-run=client` or `helm template`.
- Air-gapped targets: no internet at runtime — vendor dependencies, use internal registries/mirrors, avoid `curl | bash`.
- Finish with exact commands to verify and to roll back.
