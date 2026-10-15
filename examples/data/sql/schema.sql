-- Small relational sample for the Structured engine + structured pipeline.
-- orders references customers and products (foreign keys → graph edges).

CREATE TABLE customers (
  id          varchar(20) PRIMARY KEY,
  full_name   varchar(120),
  city        varchar(80),
  signup_date date
);

CREATE TABLE products (
  id     varchar(20) PRIMARY KEY,
  name   varchar(120),
  price  numeric(10,2)
);

CREATE TABLE orders (
  id           serial PRIMARY KEY,
  customer_id  varchar(20) REFERENCES customers(id),
  product_id   varchar(20) REFERENCES products(id),
  amount       numeric(10,2),
  status       varchar(20),
  review_text  text,
  created_at   timestamp
);

INSERT INTO customers VALUES
  ('C-1','Alex Rivera','Portland','2025-11-02'),
  ('C-2','Sam Okoye','Austin','2025-12-15'),
  ('C-3','Jo Park','Seattle','2026-01-20');

INSERT INTO products VALUES
  ('P-3','Trail Mix 1kg',54.00),
  ('P-9','Wireless Charger',129.90);

INSERT INTO orders (customer_id, product_id, amount, status, review_text, created_at) VALUES
  ('C-1','P-9',129.90,'shipped','Arrived early and the packaging was excellent.', '2026-03-01'),
  ('C-2','P-3',54.00,'pending','Support was responsive when I asked.', '2026-03-02'),
  ('C-1','P-3',54.00,'delivered','Consistent quality, fast shipping.', '2026-03-04');
