# 6. Adopt a GraphQL gateway for the web frontend

Date: 2025-04-03

## Status

Proposed

## Context

The web frontend calls five REST services per page and over-fetches.

## Decision

Introduce a GraphQL gateway that federates the service APIs for the frontend.

## Consequences

Under discussion: who owns the gateway, and how errors are mapped.
