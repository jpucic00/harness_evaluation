# shop service architecture

The service is split into layers. A module may import only from its own layer or
the layers listed under "may import".

| Layer | Package | Purpose | May import |
| --- | --- | --- | --- |
| API | `shop.api` | HTTP handlers: parse requests, call services, render responses | services, domain |
| Services | `shop.services` | Use cases and business logic | repositories, adapters, domain, and the shared cache `shop.infra.database.cache` |
| Repositories | `shop.repositories` | Persistence of domain objects | infra, domain |
| Adapters | `shop.adapters` | Clients for external systems (carrier API, payment provider) | domain |
| Infra | `shop.infra` | Database connection, configuration | (nothing in shop) |
| Domain | `shop.domain` | Entities and value objects | (nothing in shop; standard library only) |

## Rules

1. **Layering.** Imports follow the table above. In particular the API layer never
   uses repositories directly, and lower layers never import higher ones.
2. **Pure domain.** `shop.domain` depends on nothing but the standard library: no
   database, HTTP or framework imports.
3. **No cycles.** Modules must not import each other directly or indirectly.
4. **No shared mutable state in services.** Services run in many threads; they keep
   no module-level mutable state (caches, registries). Use the cache in
   `shop.infra` if you need one.
5. **External calls through adapters.** Only `shop.adapters` talks to external
   systems over the network.
6. **No per-row queries.** Code that needs related rows for a list of entities
   loads them with one batched repository call, not one query per entity.
