# 2. Use PostgreSQL as the primary datastore

Date: 2024-09-10

## Status

Accepted

## Context

Orders, customers and the product catalog are relational and change together:
placing an order reserves catalog stock in the same transaction.

## Decision

All services store their data in PostgreSQL 16 (one logical database per
service, managed by the platform team).

## Consequences

Cross-entity changes can use a single transaction. We need a migration tool
(we picked Flyway).
