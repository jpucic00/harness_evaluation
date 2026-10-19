# The pull request under review: branch feature/order-search, checked out.
git config user.name "Sam Contributor"
git config user.email "sam@example.com"
git config commit.gpgsign false
J=src/main/java/com/example/orders
git switch -q -c feature/order-search

cat > $J/OrderRepository.java <<'EOF'
package com.example.orders;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import javax.sql.DataSource;

public class OrderRepository {
    private final DataSource dataSource;

    public OrderRepository(DataSource dataSource) {
        this.dataSource = dataSource;
    }

    public Optional<Order> findById(long id) throws SQLException {
        String sql = "SELECT id, customer_id, status, total, created_at FROM orders WHERE id = ?";
        try (Connection conn = dataSource.getConnection();
                PreparedStatement stmt = conn.prepareStatement(sql)) {
            stmt.setLong(1, id);
            try (ResultSet rs = stmt.executeQuery()) {
                return rs.next() ? Optional.of(map(rs)) : Optional.empty();
            }
        }
    }

    public List<Order> findByStatus(String status) throws SQLException {
        String sql = "SELECT id, customer_id, status, total, created_at FROM orders WHERE status = ?";
        List<Order> orders = new ArrayList<>();
        try (Connection conn = dataSource.getConnection();
                PreparedStatement stmt = conn.prepareStatement(sql)) {
            stmt.setString(1, status);
            try (ResultSet rs = stmt.executeQuery()) {
                while (rs.next()) {
                    orders.add(map(rs));
                }
            }
        }
        return orders;
    }

    public List<Order> searchByCustomerName(String name) {
        List<Order> orders = new ArrayList<>();
        try {
            Connection conn = dataSource.getConnection();
            Statement stmt = conn.createStatement();
            ResultSet rs = stmt.executeQuery(
                    "SELECT o.id, o.customer_id, o.status, o.total, o.created_at FROM orders o "
                            + "JOIN customers c ON c.id = o.customer_id WHERE c.name LIKE '%"
                            + name + "%'");
            while (rs.next()) {
                orders.add(map(rs));
            }
        } catch (SQLException e) {
        }
        return orders;
    }

    static Order map(ResultSet rs) throws SQLException {
        return new Order(
                rs.getLong("id"),
                rs.getLong("customer_id"),
                rs.getString("status"),
                rs.getBigDecimal("total"),
                rs.getTimestamp("created_at").toInstant());
    }
}
EOF

cat > $J/OrderService.java <<'EOF'
package com.example.orders;

import java.math.BigDecimal;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;
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

    /** Orders matching a customer name, with the customer's display name. */
    public List<OrderSummary> search(String customerName) {
        List<OrderSummary> result = new ArrayList<>();
        for (Order order : repository.searchByCustomerName(customerName)) {
            CustomerClient.Customer customer = customers.fetch(order.customerId());
            result.add(OrderSummary.of(order, customer.name()));
        }
        return result;
    }

    /** One page (0-based) of at most size items. */
    public static <T> List<T> page(List<T> items, int page, int size) {
        int from = page * size;
        int to = Math.min(from + size, items.size());
        List<T> out = new ArrayList<>();
        for (int i = from; i <= to; i++) {
            out.add(items.get(i));
        }
        return out;
    }

    /** Sum of the totals of paid orders. */
    public static BigDecimal paidRevenue(List<Order> orders) {
        BigDecimal sum = BigDecimal.ZERO;
        for (Order order : orders) {
            if (order.status() == "PAID") {
                sum = sum.add(order.total());
            }
        }
        return sum;
    }
}
EOF

cat > $J/OrderSummary.java <<'EOF'
package com.example.orders;

import java.math.BigDecimal;
import java.util.Objects;

/** Read model for the order search screen. */
public record OrderSummary(long orderId, String customerName, String status, BigDecimal total) {
    public OrderSummary {
        Objects.requireNonNull(customerName, "customerName");
        Objects.requireNonNull(total, "total");
    }

    static OrderSummary of(Order order, String customerName) {
        return new OrderSummary(order.id(), customerName, order.status(), order.total());
    }
}
EOF

cat > $J/OrderKey.java <<'EOF'
package com.example.orders;

/** Cache key for order lookups (used as a HashMap key). */
public final class OrderKey {
    private final long customerId;
    private final long orderId;

    public OrderKey(long customerId, long orderId) {
        this.customerId = customerId;
        this.orderId = orderId;
    }

    @Override
    public boolean equals(Object other) {
        if (this == other) {
            return true;
        }
        if (!(other instanceof OrderKey key)) {
            return false;
        }
        return customerId == key.customerId && orderId == key.orderId;
    }

    @Override
    public String toString() {
        return customerId + "/" + orderId;
    }
}
EOF

cat > $J/ReportFormatter.java <<'EOF'
package com.example.orders;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;

/** Formats order lines for the CSV report. Called from request threads. */
public final class ReportFormatter {
    private static final SimpleDateFormat DATE = new SimpleDateFormat("yyyy-MM-dd");

    private ReportFormatter() {}

    public static String line(Order order) {
        return order.id() + "," + DATE.format(Date.from(order.createdAt())) + "," + order.total();
    }

    public static String header(List<String> columns) {
        return String.join(",", columns);
    }
}
EOF

git add -A
git commit -q -m "feat: order search by customer name, paging and CSV report"
