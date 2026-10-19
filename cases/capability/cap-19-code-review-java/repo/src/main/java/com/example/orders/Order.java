package com.example.orders;

import java.math.BigDecimal;
import java.time.Instant;

public record Order(long id, long customerId, String status, BigDecimal total, Instant createdAt) {}
