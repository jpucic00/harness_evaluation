# Checkout platform architecture

Last reviewed: 2026-06-30. Owner: platform architecture guild.

## 1. Overview

Customers check out through the web and mobile apps, which call the API gateway.
The gateway routes checkout traffic to **checkout-api**, which orchestrates the
checkout. Everything behind checkout-api is internal.

```
apps -> api-gateway -> checkout-api --gRPC--> order-service --gRPC--> payment-service
                                                   |
                                                   +-- Kafka: orders.v1 --> fulfillment-service --> Kafka: shipments
                                                                       +--> notification-service
                                                                       +--> analytics-ingest
```

## 2. Services

| Service | Responsibility | Datastore | Team (on call) |
| --- | --- | --- | --- |
| api-gateway | TLS, auth, routing, rate limits | none | team-edge |
| checkout-api | Orchestrates a checkout: validates the cart, places the order | none (stateless) | team-checkout |
| order-service | Owns orders and their state machine; system of record for order state | PostgreSQL (`orders` database) | team-orders |
| payment-service | Authorises and captures payments through provider adapters | PostgreSQL (`payments` database) | team-payments |
| inventory-service | Stock levels and reservations | Redis (with AOF persistence) | team-stock |
| fulfillment-service | Picks, packs and ships orders | PostgreSQL (`fulfillment` database) | team-logistics |
| notification-service | Emails and push notifications to customers | none | team-engagement |
| analytics-ingest | Loads events into the warehouse | S3 + Snowflake | team-data |

## 3. Communication

- **Synchronous:** api-gateway → checkout-api (HTTPS), checkout-api → order-service
  (gRPC), order-service → payment-service (gRPC). checkout-api never calls
  payment-service itself; payment always goes through order-service.
  checkout-api → inventory-service (gRPC) reserves stock before the order is placed.
- **Asynchronous:** order-service publishes order events (`OrderPlaced`,
  `OrderPaid`, `OrderCancelled`) to the Kafka topic `orders.v1`. fulfillment-service,
  notification-service and analytics-ingest consume `OrderPlaced`; only
  fulfillment-service and analytics-ingest consume `OrderPaid`.
  fulfillment-service publishes `ShipmentCreated` to `shipments.v1`.

Topic names in this document can lag behind deployments. The topic constants in
each service's `*_config.py` are the source of truth.

## 4. Data ownership

Each database belongs to exactly one service. **Only the owning service may read or
write its database**; every other service must use the owner's API or its events.
In particular, only order-service may read or write the `orders` database.

## 5. Availability and latency

Service level objectives (monthly availability):

| Service | SLO |
| --- | --- |
| api-gateway | 99.99 % |
| checkout-api | 99.95 % |
| inventory-service | 99.9 % |
| order-service | 99.95 % |
| payment-service | 99.9 % |
| fulfillment-service | 99.5 % |
| notification-service | 99.0 % |

A checkout request is successful only if every service it calls synchronously
succeeds. Asynchronous consumers are not on the request path.

The p99 latency budget for a checkout request, measured at the gateway, is
**900 ms**. Each hop has a fixed budget for its own work: api-gateway 40 ms,
checkout-api 120 ms, inventory-service 90 ms, order-service 200 ms. The rest of the
budget belongs to payment-service, including the provider call.

## 6. Failure modes

- **payment-service unavailable:** order-service still creates the order, in state
  `PENDING_PAYMENT`, and returns it to checkout-api. A retry job in order-service
  retries the payment with exponential backoff for up to 30 minutes, then moves
  the order to `CANCELLED` and releases the stock reservation.
- **inventory-service unavailable:** checkout-api fails the checkout with 503; no
  order is created.
- **Kafka unavailable:** order-service writes events to an outbox table and
  publishes them once Kafka is back.

## 7. Extension points

- New payment providers are adapters in payment-service (`providers/`); nothing
  else changes.
- New notification channels are plugins in notification-service.

## 8. Personal data

Customer personal data (names, addresses, emails) may be kept by analytics for
at most 30 days, after which analytics-ingest pseudonymises it. order-service keeps
it for as long as the customer account exists.
