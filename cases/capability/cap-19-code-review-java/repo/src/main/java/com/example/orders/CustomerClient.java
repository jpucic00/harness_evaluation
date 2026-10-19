package com.example.orders;

import java.util.List;
import java.util.Map;

/** HTTP client for the customer service. */
public interface CustomerClient {
    /** One HTTP call per invocation. */
    Customer fetch(long customerId);

    /** One HTTP call for all ids. */
    Map<Long, Customer> fetchAll(List<Long> customerIds);

    record Customer(long id, String name) {}
}
