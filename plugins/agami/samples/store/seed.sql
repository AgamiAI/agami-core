-- ============================================================================
-- agami sample database — "Acme Store" (retail + subscriptions)
-- ============================================================================
-- A small, self-contained SQLite dataset for onboarding agami WITHOUT a live DB.
-- Two subject areas (Commerce + Subscriptions) chosen so a first-time user sees
-- every feature: cross-area joins, fan/chasm-trap detection, enum breakdowns,
-- sensitive-column flagging, and ~24 months of dated rows for trends.
--
-- Build (either works — no Python needed for the CLI path):
--   sqlite3 store.db < seed.sql
--   python3 build_sample.py --out store.db
--
-- DESIGN RULES (keep these true if you edit):
--   * 100% DETERMINISTIC — no random()/randomblob(). Every value is a pure
--     function of a row index, so the built .db is byte-reproducible across
--     machines and runs. This is what lets us freeze the prebuilt model.
--   * SYNTHETIC ONLY — generic names + @example.com emails + 555 phone numbers.
--     Never a real person, customer, or internal term.
--
-- Trust-layer properties this dataset is built to exercise:
--   * Declared FKs (REFERENCES) on every child table  → FK auto-approve path
--   * Unique natural keys (email, sku)                 → unique_index_match signal
--   * Enum-ish columns (status, channel, tier, method) → enum_like_distribution
--   * email / phone / full_name                        → sensitive/PII flagging
--   * FAN TRAP:   orders → order_items (1:N) AND orders → payments (1:N)
--                 summing payments while joined to items double-counts
--   * CHASM TRAP: customers → orders (1:N) AND customers → subscriptions (1:N)
--                 joining orders to subscriptions via customer multiplies rows
-- ============================================================================

PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS subscription_events;
DROP TABLE IF EXISTS subscriptions;
DROP TABLE IF EXISTS plans;
DROP TABLE IF EXISTS refunds;
DROP TABLE IF EXISTS payments;
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS categories;
DROP TABLE IF EXISTS customers;

-- ---------------------------------------------------------------------------
-- Schema
-- ---------------------------------------------------------------------------

CREATE TABLE categories (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL UNIQUE,
    slug   TEXT NOT NULL UNIQUE
);

CREATE TABLE products (
    id           INTEGER PRIMARY KEY,
    sku          TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    category_id  INTEGER NOT NULL REFERENCES categories(id),
    unit_price   REAL NOT NULL,
    is_active    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE customers (
    id           INTEGER PRIMARY KEY,
    email        TEXT NOT NULL UNIQUE,   -- PII
    full_name    TEXT NOT NULL,          -- PII
    phone        TEXT,                   -- PII
    country      TEXT NOT NULL,
    signup_date  TEXT NOT NULL,
    is_active    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE orders (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    status       TEXT NOT NULL,          -- pending|paid|shipped|delivered|cancelled|refunded
    channel      TEXT NOT NULL,          -- web|mobile|store|partner
    total_amount REAL NOT NULL DEFAULT 0,-- order-grain total (= SUM of its order_items); the FAN-TRAP measure
    placed_at    TEXT NOT NULL,
    shipped_at   TEXT
);

CREATE TABLE order_items (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    product_id  INTEGER NOT NULL REFERENCES products(id),
    quantity    INTEGER NOT NULL,
    unit_price  REAL NOT NULL
);

CREATE TABLE payments (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    method      TEXT NOT NULL,           -- card|paypal|bank_transfer|apple_pay
    amount      REAL NOT NULL,
    status      TEXT NOT NULL,           -- captured|failed|refunded
    paid_at     TEXT NOT NULL
);

CREATE TABLE refunds (
    id           INTEGER PRIMARY KEY,
    payment_id   INTEGER NOT NULL REFERENCES payments(id),
    amount       REAL NOT NULL,
    reason       TEXT NOT NULL,          -- requested|damaged|fraud|not_received
    refunded_at  TEXT NOT NULL
);

CREATE TABLE plans (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    tier           TEXT NOT NULL,        -- free|pro|business|enterprise
    monthly_price  REAL NOT NULL,
    is_active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE subscriptions (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    plan_id      INTEGER NOT NULL REFERENCES plans(id),
    status       TEXT NOT NULL,          -- trialing|active|paused|canceled
    started_at   TEXT NOT NULL,
    canceled_at  TEXT
);

CREATE TABLE subscription_events (
    id               INTEGER PRIMARY KEY,
    subscription_id  INTEGER NOT NULL REFERENCES subscriptions(id),
    event_type       TEXT NOT NULL,      -- created|upgraded|downgraded|renewed|canceled
    from_plan_id     INTEGER REFERENCES plans(id),
    to_plan_id       INTEGER REFERENCES plans(id),
    occurred_at      TEXT NOT NULL
);

CREATE TABLE invoices (
    id               INTEGER PRIMARY KEY,
    subscription_id  INTEGER NOT NULL REFERENCES subscriptions(id),
    amount           REAL NOT NULL,
    status           TEXT NOT NULL,      -- paid|open|void
    issued_at        TEXT NOT NULL,
    paid_at          TEXT
);

CREATE INDEX idx_products_category      ON products(category_id);
CREATE INDEX idx_orders_customer        ON orders(customer_id);
CREATE INDEX idx_orders_placed          ON orders(placed_at);
CREATE INDEX idx_order_items_order      ON order_items(order_id);
CREATE INDEX idx_order_items_product    ON order_items(product_id);
CREATE INDEX idx_payments_order         ON payments(order_id);
CREATE INDEX idx_refunds_payment        ON refunds(payment_id);
CREATE INDEX idx_subscriptions_customer ON subscriptions(customer_id);
CREATE INDEX idx_subscriptions_plan     ON subscriptions(plan_id);
CREATE INDEX idx_sub_events_sub         ON subscription_events(subscription_id);
CREATE INDEX idx_invoices_sub           ON invoices(subscription_id);

-- ---------------------------------------------------------------------------
-- Reference data (small, explicit)
-- ---------------------------------------------------------------------------

INSERT INTO categories (id, name, slug) VALUES
    (1, 'Electronics',  'electronics'),
    (2, 'Home & Kitchen','home-kitchen'),
    (3, 'Apparel',      'apparel'),
    (4, 'Sports & Outdoors','sports-outdoors'),
    (5, 'Books',        'books'),
    (6, 'Beauty',       'beauty'),
    (7, 'Toys & Games', 'toys-games'),
    (8, 'Office',       'office');

INSERT INTO plans (id, name, tier, monthly_price, is_active) VALUES
    (1, 'Free',       'free',        0.00,  1),
    (2, 'Pro',        'pro',         29.00, 1),
    (3, 'Business',   'business',    99.00, 1),
    (4, 'Enterprise', 'enterprise',  499.00,1),
    (5, 'Legacy Plus','pro',         19.00, 0);

-- ---------------------------------------------------------------------------
-- Products — 64 rows, 8 per category. Price is a deterministic function of id.
-- ---------------------------------------------------------------------------
WITH RECURSIVE seq(n) AS (
    SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 64
),
adjective(i, w) AS (
    VALUES (0,'Compact'),(1,'Deluxe'),(2,'Premium'),(3,'Eco'),
           (4,'Smart'),(5,'Classic'),(6,'Pro'),(7,'Ultra')
),
noun(i, w) AS (
    VALUES (0,'Speaker'),(1,'Blender'),(2,'Jacket'),(3,'Tent'),
           (4,'Novel'),(5,'Serum'),(6,'Puzzle'),(7,'Organizer')
)
INSERT INTO products (id, sku, name, category_id, unit_price, is_active)
SELECT
    n,
    'SKU-' || substr('0000' || n, -4),
    a.w || ' ' || no.w || ' ' || n,
    1 + ((n - 1) % 8),
    -- $4.99 .. ~$399.99, deterministic spread
    round(4.99 + ((n * 37) % 395) + (((n * 7) % 100) / 100.0), 2),
    CASE WHEN n % 17 = 0 THEN 0 ELSE 1 END
FROM seq
JOIN adjective a ON a.i = (n * 3) % 8
JOIN noun no     ON no.i = (n * 5) % 8;

-- ---------------------------------------------------------------------------
-- Customers — 500 rows. email/full_name/phone are synthetic PII.
-- signup_date spread over ~3 years ending before the order window.
-- ---------------------------------------------------------------------------
WITH RECURSIVE seq(n) AS (
    SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 500
),
-- 25 first names × 20 last names, indexed as a bijection of (n-1) below, so all
-- 500 customers get a DISTINCT full name (no collisions). 25 * 20 = 500.
firsts(i, w) AS (
    VALUES (0,'Avery'),(1,'Blake'),(2,'Casey'),(3,'Devon'),(4,'Emery'),
           (5,'Finley'),(6,'Gray'),(7,'Harper'),(8,'Indra'),(9,'Jules'),
           (10,'Kai'),(11,'Logan'),(12,'Morgan'),(13,'Noor'),(14,'Oakley'),
           (15,'Parker'),(16,'Quinn'),(17,'Riley'),(18,'Sage'),(19,'Tatum'),
           (20,'Uma'),(21,'Vihaan'),(22,'Wren'),(23,'Xiomara'),(24,'Yael')
),
lasts(i, w) AS (
    VALUES (0,'Adams'),(1,'Bauer'),(2,'Cruz'),(3,'Diaz'),(4,'Evans'),
           (5,'Flores'),(6,'Green'),(7,'Hughes'),(8,'Ito'),(9,'Jensen'),
           (10,'Khan'),(11,'Lopez'),(12,'Mori'),(13,'Novak'),(14,'Owens'),
           (15,'Patel'),(16,'Reyes'),(17,'Singh'),(18,'Tran'),(19,'Vogel')
),
countries(i, w) AS (
    VALUES (0,'US'),(1,'US'),(2,'US'),(3,'UK'),(4,'DE'),
           (5,'FR'),(6,'CA'),(7,'AU')
)
INSERT INTO customers (id, email, full_name, phone, country, signup_date, is_active)
SELECT
    n,
    'customer' || n || '@example.com',
    f.w || ' ' || l.w,
    '+1-555-' || substr('0000' || ((n * 53) % 10000), -4),
    c.w,
    date('2022-01-01', '+' || ((n * 211) % 900) || ' days'),
    CASE WHEN n % 11 = 0 THEN 0 ELSE 1 END
FROM seq
-- base-25 decomposition of (n-1): unique (first,last) pair per customer, and the
-- first name cycles every row (not blocky) so consecutive customers look distinct.
JOIN firsts    f ON f.i = (n - 1) % 25
JOIN lasts     l ON l.i = (n - 1) / 25
JOIN countries c ON c.i = n % 8;

-- ---------------------------------------------------------------------------
-- Orders — 4000 rows over 2024-06-01 .. 2026-05-31 (~24 months).
-- customer_id spans 1..500 (overlaps subscription customers → chasm trap).
-- status/channel are deterministic enum distributions.
-- ---------------------------------------------------------------------------
WITH RECURSIVE seq(n) AS (
    SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 4000
)
INSERT INTO orders (id, customer_id, status, channel, placed_at, shipped_at)
SELECT
    n,
    1 + ((n * 109) % 500),
    CASE
        WHEN n % 20 < 10 THEN 'delivered'
        WHEN n % 20 < 14 THEN 'shipped'
        WHEN n % 20 < 16 THEN 'paid'
        WHEN n % 20 < 17 THEN 'pending'
        WHEN n % 20 < 18 THEN 'cancelled'
        ELSE 'refunded'
    END,
    CASE
        WHEN n % 10 < 5 THEN 'web'
        WHEN n % 10 < 8 THEN 'mobile'
        WHEN n % 10 < 9 THEN 'store'
        ELSE 'partner'
    END,
    -- spread across 730 days; (n*181)%730 is a near-even permutation
    datetime('2024-06-01 00:00:00', '+' || ((n * 181) % 730) || ' days',
             '+' || ((n * 7) % 24) || ' hours'),
    CASE
        WHEN n % 20 < 14   -- delivered + shipped get a ship date
        THEN datetime('2024-06-01 00:00:00', '+' || (((n * 181) % 730) + 1 + (n % 4)) || ' days')
        ELSE NULL
    END
FROM seq;

-- ---------------------------------------------------------------------------
-- Order items — fan-out 1..4 per order (1 + order_id % 4). ~10k rows.
-- This is the FAN-TRAP arm: orders → order_items is 1:N.
-- ---------------------------------------------------------------------------
WITH RECURSIVE k(j) AS (
    SELECT 1 UNION ALL SELECT j + 1 FROM k WHERE j < 4
)
INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT
    o.id,
    p.id,
    1 + ((o.id * j) % 5),
    p.unit_price
FROM orders o
JOIN k        ON k.j <= 1 + (o.id % 4)
JOIN products p ON p.id = 1 + ((o.id * 31 + j * 17) % 64);

-- Denormalize the order total onto orders (order-grain). Summing THIS across a
-- join to order_items/products (line-item grain) is the classic fan trap.
UPDATE orders
SET total_amount = (
    SELECT round(COALESCE(SUM(oi.quantity * oi.unit_price), 0), 2)
    FROM order_items oi WHERE oi.order_id = orders.id
);

-- ---------------------------------------------------------------------------
-- Payments — one per non-cancelled order. amount = sum of its order_items.
-- This is the OTHER FAN-TRAP arm: orders → payments is 1:N (here ~1:1, but the
-- model marks it 1:N, so joining payments to order_items via orders multiplies).
-- ---------------------------------------------------------------------------
INSERT INTO payments (order_id, method, amount, status, paid_at)
SELECT
    o.id,
    CASE o.id % 4 WHEN 0 THEN 'card' WHEN 1 THEN 'paypal'
                  WHEN 2 THEN 'apple_pay' ELSE 'bank_transfer' END,
    (SELECT round(COALESCE(SUM(oi.quantity * oi.unit_price), 0), 2)
       FROM order_items oi WHERE oi.order_id = o.id),
    CASE o.status WHEN 'refunded' THEN 'refunded'
                  WHEN 'pending'  THEN 'failed'
                  ELSE 'captured' END,
    o.placed_at
FROM orders o
WHERE o.status <> 'cancelled';

-- ---------------------------------------------------------------------------
-- Refunds — for every refunded payment, plus a few goodwill partials.
-- ---------------------------------------------------------------------------
INSERT INTO refunds (payment_id, amount, reason, refunded_at)
SELECT
    p.id,
    -- Half refunds computed in integer cents (CAST truncates deterministically) so the
    -- value never lands on a half-cent ROUND boundary that differs across SQLite versions.
    CASE WHEN p.id % 3 = 0
         THEN (CAST(round(p.amount * 100) AS INTEGER) / 2) / 100.0
         ELSE p.amount END,
    CASE p.id % 4 WHEN 0 THEN 'damaged' WHEN 1 THEN 'not_received'
                  WHEN 2 THEN 'fraud' ELSE 'requested' END,
    datetime(p.paid_at, '+' || (3 + (p.id % 10)) || ' days')
FROM payments p
WHERE p.status = 'refunded';

-- ---------------------------------------------------------------------------
-- Subscriptions — 400 rows. customer_id 1..500 overlaps order customers, so a
-- customer can have BOTH orders and a subscription → CHASM TRAP.
-- ---------------------------------------------------------------------------
WITH RECURSIVE seq(n) AS (
    SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < 400
)
INSERT INTO subscriptions (id, customer_id, plan_id, status, started_at, canceled_at)
SELECT
    n,
    1 + ((n * 7) % 500),
    1 + (n % 4),
    CASE
        WHEN n % 10 < 6 THEN 'active'
        WHEN n % 10 < 8 THEN 'trialing'
        WHEN n % 10 < 9 THEN 'paused'
        ELSE 'canceled'
    END,
    date('2024-06-01', '+' || ((n * 281) % 700) || ' days'),
    CASE WHEN n % 10 = 9
         THEN date('2024-06-01', '+' || (((n * 281) % 700) + 30 + (n % 120)) || ' days')
         ELSE NULL END
FROM seq;

-- ---------------------------------------------------------------------------
-- Subscription events — a 'created' for each, plus upgrades/cancels for some.
-- ---------------------------------------------------------------------------
INSERT INTO subscription_events (subscription_id, event_type, from_plan_id, to_plan_id, occurred_at)
SELECT s.id, 'created', NULL, s.plan_id, s.started_at
FROM subscriptions s;

INSERT INTO subscription_events (subscription_id, event_type, from_plan_id, to_plan_id, occurred_at)
SELECT
    s.id,
    CASE WHEN s.plan_id < 4 THEN 'upgraded' ELSE 'downgraded' END,
    s.plan_id,
    CASE WHEN s.plan_id < 4 THEN s.plan_id + 1 ELSE s.plan_id - 1 END,
    date(s.started_at, '+' || (45 + (s.id % 90)) || ' days')
FROM subscriptions s
WHERE s.id % 3 = 0;

INSERT INTO subscription_events (subscription_id, event_type, from_plan_id, to_plan_id, occurred_at)
SELECT s.id, 'canceled', s.plan_id, NULL, s.canceled_at
FROM subscriptions s
WHERE s.canceled_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Invoices — up to 12 monthly invoices per subscription, capped at "today".
-- ~3-4k rows. Most paid; the latest open; a few void.
-- ---------------------------------------------------------------------------
WITH RECURSIVE m(k) AS (
    SELECT 0 UNION ALL SELECT k + 1 FROM m WHERE k < 11
)
INSERT INTO invoices (subscription_id, amount, status, issued_at, paid_at)
SELECT
    s.id,
    p.monthly_price,
    CASE
        WHEN m.k = 0 AND (s.id + m.k) % 13 = 0 THEN 'void'
        WHEN date(s.started_at, '+' || (m.k + 1) || ' months') > date('2026-06-01') THEN 'open'
        ELSE 'paid'
    END,
    date(s.started_at, '+' || m.k || ' months'),
    CASE
        WHEN date(s.started_at, '+' || (m.k + 1) || ' months') > date('2026-06-01') THEN NULL
        WHEN (s.id + m.k) % 13 = 0 AND m.k = 0 THEN NULL
        ELSE date(s.started_at, '+' || m.k || ' months', '+2 days')
    END
FROM subscriptions s
JOIN plans p ON p.id = s.plan_id
JOIN m ON date(s.started_at, '+' || m.k || ' months') <= date('2026-06-01')
WHERE p.monthly_price > 0;
