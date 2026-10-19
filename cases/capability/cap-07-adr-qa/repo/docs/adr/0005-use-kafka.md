# 5. Use Kafka for event streaming

Date: 2025-02-20

## Status

Accepted on 2025-03-11. Supersedes [3. Use RabbitMQ](0003-use-rabbitmq.md).

## Context

Analytics and the new fulfillment service need to replay order history, which
RabbitMQ (ADR 3) cannot do once messages are consumed.

## Decision

Services publish domain events to Kafka (Amazon MSK). Order events are written
to the `orders.v1` topic, partitioned by `order_id` so that all events of one
order stay in order. Topics keep events for 7 days; longer history is archived
to S3 by the analytics team.

## Consequences

RabbitMQ is decommissioned by the end of Q2 2025. New services must not use it.
