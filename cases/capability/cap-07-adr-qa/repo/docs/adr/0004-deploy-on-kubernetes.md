# 4. Deploy on Kubernetes

Date: 2024-11-15

## Status

Accepted

## Context

We run eight services and want uniform deployment, scaling and secrets handling.

## Decision

Every service is deployed to the shared EKS cluster with Helm charts from the
platform team. Each service gets its own namespace.

## Consequences

Teams need basic Kubernetes knowledge. Local development uses kind.
