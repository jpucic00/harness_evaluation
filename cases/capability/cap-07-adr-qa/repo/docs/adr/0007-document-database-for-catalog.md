# 7. Use a document database for the product catalog

Date: 2025-05-12

## Status

Rejected

## Context

Catalog items have many optional attributes, which a document database such as
MongoDB would store naturally.

## Decision

Rejected. Nobody on the team has run MongoDB in production, and placing an order
must reserve catalog stock in the same transaction as the order itself (see
ADR 2). Performance and cost were comparable and were not deciding factors.

## Consequences

The catalog stays in PostgreSQL; optional attributes go into a `jsonb` column.
