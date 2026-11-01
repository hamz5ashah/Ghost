---
title: Deployment
description: Deploy Ghost in production environments
---

# Deployment

Ghost supports multiple deployment strategies for different environments
and scales.

## Docker

The recommended way to deploy Ghost in production. Multi-stage builds
with CPU and GPU (NVIDIA CUDA, AMD ROCm) variants.

[:octicons-arrow-right-24: Docker deployment](docker.md)

## systemd (Linux)

Run Ghost as a managed system service on Linux servers.

[:octicons-arrow-right-24: systemd setup](systemd.md)

## launchd (macOS)

Register Ghost as a launch agent on macOS.

[:octicons-arrow-right-24: launchd setup](launchd.md)

## API Server

Run Ghost as an OpenAI-compatible HTTP server via `ghost serve`.

[:octicons-arrow-right-24: API server guide](api-server.md)
