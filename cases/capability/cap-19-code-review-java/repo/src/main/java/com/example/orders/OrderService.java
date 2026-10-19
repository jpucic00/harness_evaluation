package com.example.orders;

import java.sql.SQLException;
import java.util.Optional;

public class OrderService {
    private final OrderRepository repository;
    private final CustomerClient customers;

    public OrderService(OrderRepository repository, CustomerClient customers) {
        this.repository = repository;
        this.customers = customers;
    }

    public Optional<Order> get(long id) throws SQLException {
        return repository.findById(id);
    }
}
