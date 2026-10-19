# 3. Use RabbitMQ for asynchronous messaging

Date: 2024-10-01

## Status

Superseded by [5. Use Kafka for event streaming](0005-use-kafka.md)

## Context

Services need to notify each other about order changes without synchronous calls.

## Decision

Services publish domain events to a RabbitMQ topic exchange. Messages are
acknowledged and removed once consumed.

## Consequences

Simple to operate. Consumers cannot replay past events.
