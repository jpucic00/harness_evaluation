# 8. Stateless JWT authentication

Date: 2025-07-01

## Status

Accepted

## Context

Session lookups in a shared store add latency to every request.

## Decision

The identity service issues signed JWT access tokens valid for 15 minutes and
refresh tokens valid for 30 days. Services validate access tokens locally with
the published JWKS keys.

## Consequences

Revoking an access token takes effect only when it expires (at most 15 minutes).
