-- Shop schema for SQLite — mirrors postgres-init.sql but in SQLite syntax.
-- Used as the develop-loop substrate for the trust-layer launch (see plan §7
-- and the smoke test in tests/integration/README.md).
--
-- Build:
--   sqlite3 tests/integration/fixtures/shop.db < tests/integration/fixtures/sqlite-shop-init.sql
--
-- Notable trust-layer test properties of this fixture:
--   - FKs are declared (REFERENCES) — exercises the FK auto-approve path
--   - Unique indexes on natural keys (email, sku) — exercises the
--     unique_index_match signal
--   - NO column comments (SQLite doesn't support them) — exercises the
--     "missing DBA signal" path on field descriptions, putting most fields
--     in the medium-confidence band where they need review
--   - status / category enum-like columns — exercise enum_like_distribution

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    region      TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    category    TEXT,
    unit_price  REAL NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    status       TEXT NOT NULL,
    placed_at    TEXT NOT NULL,
    shipped_at   TEXT
);

CREATE TABLE order_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    product_id  INTEGER NOT NULL REFERENCES products(id),
    quantity    INTEGER NOT NULL,
    unit_price  REAL NOT NULL
);

CREATE INDEX idx_orders_customer    ON orders(customer_id);
CREATE INDEX idx_orders_placed      ON orders(placed_at);
CREATE INDEX idx_order_items_order  ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);

-- Customers
INSERT INTO customers (email, name, region) VALUES
    ('alice@example.com', 'Alice Anderson', 'NA'),
    ('bob@example.com',   'Bob Brown',      'EU'),
    ('carol@example.com', 'Carol Chen',     'APAC'),
    ('dave@example.com',  'Dave Davis',     'NA'),
    ('erin@example.com',  'Erin Edwards',   'EU');

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
