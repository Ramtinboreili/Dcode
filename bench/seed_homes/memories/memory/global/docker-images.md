---
name: docker-images
description: Dockerfiles: pinned base, non-root, copy requirements first
tags: docker, dockerfile, image, container
updated: 2025-01-01T00:00:00+00:00
---

Pin the base image tag. Run as a non-root user. Copy dependency files before the source so the layer cache works.
