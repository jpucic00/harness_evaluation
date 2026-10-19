"""gRPC clients used by checkout-api."""

from services.common.grpc import Channel

inventory = Channel("inventory-service:50051").stub("InventoryService")
orders = Channel("order-service:50051").stub("OrderService")
