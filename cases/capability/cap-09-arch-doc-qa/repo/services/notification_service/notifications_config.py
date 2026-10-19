ORDERS_TOPIC = "orders.v1"
# Read replica, so lookups do not load the primary.
ORDERS_REPLICA_DSN = "postgresql://notification-service@orders-db-replica/orders"
