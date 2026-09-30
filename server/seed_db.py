"""Creates a small sandboxed SQLite DB the query_database tool is allowed to
touch. Includes a deliberately sensitive column (ssn) so the audit-log
redaction logic in security.py has something real to redact."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "company.db"


def seed():
    if DB_PATH.exists():
        return  # idempotent — don't clobber data on every server start

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            ssn TEXT NOT NULL
        );

        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            order_date TEXT NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        INSERT INTO customers (id, name, email, ssn) VALUES
            (1, 'Amina Kader', 'amina@example.com', '123-45-6789'),
            (2, 'Tom Reyes', 'tom@example.com', '987-65-4321'),
            (3, 'Priya Nair', 'priya@example.com', '555-11-2222');

        INSERT INTO orders (id, customer_id, amount, order_date) VALUES
            (1, 1, 89.99, '2026-08-01'),
            (2, 1, 42.50, '2026-08-14'),
            (3, 2, 199.00, '2026-08-20'),
            (4, 3, 15.25, '2026-09-02');
        """
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    seed()
    print(f"Seeded demo database at {DB_PATH}")
