-- Seed schema for Postgres integration tests.
-- A small "shop" model with FK relationships and a few rows of data
-- so the introspection + NL->SQL paths have something realistic to chew on.

CREATE TABLE customers (
    id           BIGSERIAL PRIMARY KEY,
    email        VARCHAR(255) NOT NULL UNIQUE,
    name         VARCHAR(255) NOT NULL,
    region       VARCHAR(50),
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE products (
    id           BIGSERIAL PRIMARY KEY,
    sku          VARCHAR(50) NOT NULL UNIQUE,
    name         VARCHAR(255) NOT NULL,
    category     VARCHAR(50),
    unit_price   NUMERIC(10, 2) NOT NULL,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE orders (
    id            BIGSERIAL PRIMARY KEY,
    customer_id   BIGINT NOT NULL REFERENCES customers(id),
    status        VARCHAR(20) NOT NULL,
    placed_at     TIMESTAMP NOT NULL,
    shipped_at    TIMESTAMP
);

CREATE TABLE order_items (
    id           BIGSERIAL PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES orders(id),
    product_id   BIGINT NOT NULL REFERENCES products(id),
    quantity     INTEGER NOT NULL,
    unit_price   NUMERIC(10, 2) NOT NULL
);

CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_placed ON orders(placed_at);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);

-- Customers
INSERT INTO customers (email, name, region) VALUES
    ('alice@example.com',   'Alice Anderson', 'NA'),
    ('bob@example.com',     'Bob Brown',      'EU'),
    ('carol@example.com',   'Carol Chen',     'APAC'),
    ('dave@example.com',    'Dave Davis',     'NA'),
    ('erin@example.com',    'Erin Edwards',   'EU');

-- Products
INSERT INTO products (sku, name, category, unit_price) VALUES
    ('SKU-001', 'Widget',          'Hardware', 9.99),
    ('SKU-002', 'Gadget',          'Hardware', 19.99),
    ('SKU-003', 'Sprocket',        'Hardware', 4.50),
    ('SKU-004', 'Cog',             'Hardware', 7.25),
    ('SKU-005', 'Premium Service', 'Service',  99.00);

-- Orders
INSERT INTO orders (customer_id, status, placed_at, shipped_at) VALUES
    (1, 'shipped',   '2026-04-01 10:00:00', '2026-04-02 09:00:00'),
    (1, 'delivered', '2026-04-15 11:30:00', '2026-04-16 10:00:00'),
    (2, 'shipped',   '2026-04-20 14:00:00', '2026-04-22 12:00:00'),
    (3, 'pending',   '2026-05-01 09:15:00', NULL),
    (4, 'shipped',   '2026-05-03 16:45:00', '2026-05-04 11:00:00'),
    (5, 'cancelled', '2026-05-04 08:00:00', NULL);

-- Order items
INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES
    (1, 1, 3, 9.99),
    (1, 2, 1, 19.99),
    (2, 3, 10, 4.50),
    (3, 1, 5, 9.99),
    (3, 5, 1, 99.00),
    (4, 2, 2, 19.99),
    (5, 4, 8, 7.25),
    (5, 1, 1, 9.99);
