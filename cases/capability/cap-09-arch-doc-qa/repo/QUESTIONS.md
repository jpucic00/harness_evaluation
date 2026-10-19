# Architecture review questions

Answer in `answers.json`. Formats: *service* = a service name exactly as written in
the architecture document (e.g. `"order-service"`), *services* = a JSON list of
those, *number* = a JSON number, *string* = a JSON string, *letter* = one of
`"a"`, `"b"`, `"c"`, `"d"`.

| Key | Question | Format |
| --- | --- | --- |
| q1 | Which service is the system of record for an order's state? | service |
| q2 | Which service calls payment-service? | service |
| q3 | Which services consume the OrderPlaced event? | services |
| q4 | What happens to a checkout while payment-service is down? a) the checkout fails with an error, b) the order is created as PENDING_PAYMENT and payment is retried for up to 30 minutes before the order is cancelled, c) the order is created and payment is skipped, d) the request is queued in checkout-api until payment-service recovers | letter |
| q5 | What availability (in percent, rounded to two decimals) does the checkout request path have if each synchronous dependency on it just meets its SLO? | number |
| q6 | How many milliseconds of the p99 checkout latency budget are left for payment-service? | number |
| q7 | Which datastore does inventory-service use? Answer with the product name in lowercase. | string |
| q8 | Which Kafka topic does fulfillment-service actually publish ShipmentCreated to? | string |
| q9 | Which service's code breaks the data-ownership rule? | service |
| q10 | Which services may write to the orders database? | services |
| q11 | For how many days may analytics keep personal data? | number |
| q12 | Which team is on call for inventory-service? | string |
| q13 | Which service would you change to add a new payment provider? | service |
