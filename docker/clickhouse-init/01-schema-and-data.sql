-- Dummy data for end-to-end testing of the clickhouse-api.
-- Runs once on first container init as the default (full-access) user.

CREATE DATABASE IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.events
(
    event_id    UInt64,
    user_id     UInt32,
    event_type  LowCardinality(String),
    country     LowCardinality(String),
    amount      Decimal(10, 2),
    created_at  DateTime
)
ENGINE = MergeTree
ORDER BY (created_at, event_id);

INSERT INTO analytics.events
    (event_id, user_id, event_type, country, amount, created_at)
VALUES
    (1,  101, 'purchase', 'US', 49.99,  '2026-05-01 09:15:00'),
    (2,  102, 'view',     'GB', 0.00,   '2026-05-01 09:17:30'),
    (3,  101, 'refund',   'US', -49.99, '2026-05-02 14:02:10'),
    (4,  103, 'purchase', 'DE', 129.00, '2026-05-02 16:45:00'),
    (5,  104, 'signup',   'IN', 0.00,   '2026-05-03 11:30:00'),
    (6,  102, 'purchase', 'GB', 19.95,  '2026-05-03 18:20:45'),
    (7,  105, 'purchase', 'US', 250.00, '2026-05-04 08:05:00'),
    (8,  103, 'view',     'DE', 0.00,   '2026-05-04 12:00:00'),
    (9,  106, 'purchase', 'FR', 75.50,  '2026-05-05 19:10:20'),
    (10, 104, 'purchase', 'IN', 12.00,  '2026-05-05 21:55:00'),
    (11, 107, 'signup',   'US', 0.00,   '2026-05-06 07:42:00'),
    (12, 105, 'refund',   'US', -250.00,'2026-05-06 10:15:30');

-- A second, smaller table so listTables / getTableSchema have variety.
CREATE TABLE IF NOT EXISTS analytics.users
(
    user_id     UInt32,
    signup_date Date,
    plan        LowCardinality(String)
)
ENGINE = MergeTree
ORDER BY user_id;

INSERT INTO analytics.users (user_id, signup_date, plan) VALUES
    (101, '2026-01-10', 'pro'),
    (102, '2026-02-15', 'free'),
    (103, '2026-02-20', 'pro'),
    (104, '2026-03-03', 'free'),
    (105, '2026-03-18', 'enterprise'),
    (106, '2026-04-01', 'free'),
    (107, '2026-04-22', 'pro');
