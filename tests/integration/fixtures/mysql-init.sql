-- Seed schema for MySQL integration tests. Mirrors the Postgres fixture
-- so query results can be compared cross-dialect.

CREATE TABLE customers (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    email        VARCHAR(255) NOT NULL UNIQUE,
    name         VARCHAR(255) NOT NULL,
    region       VARCHAR(50),
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE products (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    sku          VARCHAR(50) NOT NULL UNIQUE,
    name         VARCHAR(255) NOT NULL,
    category     VARCHAR(50),
    unit_price   DECIMAL(10, 2) NOT NULL,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE orders (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    customer_id   BIGINT NOT NULL,
    status        VARCHAR(20) NOT NULL,
    placed_at     TIMESTAMP NOT NULL,
    shipped_at    TIMESTAMP NULL,
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(id),
    INDEX idx_orders_customer (customer_id),
    INDEX idx_orders_placed (placed_at)
);

CREATE TABLE order_items (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id     BIGINT NOT NULL,
    product_id   BIGINT NOT NULL,
    quantity     INT NOT NULL,
    unit_price   DECIMAL(10, 2) NOT NULL,
    CONSTRAINT fk_items_order   FOREIGN KEY (order_id)   REFERENCES orders(id),
    CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES products(id),
    INDEX idx_order_items_order (order_id),
    INDEX idx_order_items_product (product_id)
);

INSERT INTO customers (email, name, region) VALUES
    ('alice@example.com',   'Alice Anderson', 'NA'),
    ('bob@example.com',     'Bob Brown',      'EU'),
    ('carol@example.com',   'Carol Chen',     'APAC'),
    ('dave@example.com',    'Dave Davis',     'NA'),
    ('erin@example.com',    'Erin Edwards',   'EU');

INSERT INTO products (sku, name, category, unit_price) VALUES
    ('SKU-001', 'Widget',          'Hardware', 9.99),
    ('SKU-002', 'Gadget',          'Hardware', 19.99),
    ('SKU-003', 'Sprocket',        'Hardware', 4.50),
    ('SKU-004', 'Cog',             'Hardware', 7.25),
    ('SKU-005', 'Premium Service', 'Service',  99.00);

INSERT INTO orders (customer_id, status, placed_at, shipped_at) VALUES
    (1, 'shipped',   '2026-04-01 10:00:00', '2026-04-02 09:00:00'),
    (1, 'delivered', '2026-04-15 11:30:00', '2026-04-16 10:00:00'),
    (2, 'shipped',   '2026-04-20 14:00:00', '2026-04-22 12:00:00'),
    (3, 'pending',   '2026-05-01 09:15:00', NULL),
    (4, 'shipped',   '2026-05-03 16:45:00', '2026-05-04 11:00:00'),
    (5, 'cancelled', '2026-05-04 08:00:00', NULL);

INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES
    (1, 1, 3, 9.99),
    (1, 2, 1, 19.99),
    (2, 3, 10, 4.50),
    (3, 1, 5, 9.99),
    (3, 5, 1, 99.00),
    (4, 2, 2, 19.99),
    (5, 4, 8, 7.25),
    (5, 1, 1, 9.99);
