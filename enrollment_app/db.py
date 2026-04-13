from __future__ import annotations

import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / "instance"
DB_PATH = INSTANCE_DIR / "enrollment_app.db"


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('client', 'staff', 'admin', 'cashier')),
    phone TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS instructors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    qualifications TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    duration_days INTEGER NOT NULL,
    price_cents INTEGER NOT NULL,
    prerequisites TEXT,
    certificate_prefix TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS class_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    batch_code TEXT NOT NULL UNIQUE,
    venue TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    seat_limit INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'open', 'closed', 'completed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class_batch_id INTEGER NOT NULL REFERENCES class_batches(id) ON DELETE CASCADE,
    instructor_id INTEGER NOT NULL REFERENCES instructors(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('lead', 'support')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(class_batch_id, instructor_id)
);

CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    class_batch_id INTEGER NOT NULL REFERENCES class_batches(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('pending_payment', 'confirmed', 'completed', 'cancelled')),
    approval_status TEXT NOT NULL CHECK(approval_status IN ('pending', 'approved', 'rejected')),
    payment_status TEXT NOT NULL CHECK(payment_status IN ('pending', 'paid', 'failed', 'refunded')),
    emergency_contact_name TEXT NOT NULL,
    emergency_contact_phone TEXT NOT NULL,
    notes TEXT,
    certificate_eligible INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, class_batch_id)
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    payment_method TEXT NOT NULL DEFAULT 'gcash',
    provider_reference TEXT NOT NULL UNIQUE,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL CHECK(status IN ('pending', 'succeeded', 'failed')),
    receipt_url TEXT,
    approved_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    approved_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS attendance_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
    session_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'present', 'absent')),
    marked_by_client INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(enrollment_id, session_date)
);

CREATE TABLE IF NOT EXISTS certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enrollment_id INTEGER NOT NULL UNIQUE REFERENCES enrollments(id) ON DELETE CASCADE,
    certificate_number TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    file_path TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    INSTANCE_DIR.mkdir(exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def initialize_database() -> None:
    with get_connection() as connection:
        connection.executescript(SCHEMA)
        migrate_database(connection)


def migrate_database(connection: sqlite3.Connection) -> None:
    migrate_user_roles(connection)
    repair_stale_user_foreign_keys(connection)
    ensure_column(connection, "enrollments", "certificate_eligible", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(connection, "payments", "payment_method", "TEXT NOT NULL DEFAULT 'gcash'")
    ensure_column(connection, "payments", "approved_by_user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL")
    ensure_column(connection, "payments", "approved_at", "TEXT")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enrollment_id INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
            session_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending', 'present', 'absent')),
            marked_by_client INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(enrollment_id, session_date)
        )
        """
    )
    connection.commit()


def migrate_user_roles(connection: sqlite3.Connection) -> None:
    create_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    if not create_sql_row or "'cashier'" in (create_sql_row["sql"] or ""):
        return
    connection.executescript(
        """
        PRAGMA foreign_keys = OFF;
        ALTER TABLE users RENAME TO users_old;
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('client', 'staff', 'admin', 'cashier')),
            phone TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO users (id, full_name, email, password_hash, role, phone, active, created_at)
        SELECT id, full_name, email, password_hash, role, phone, active, created_at
        FROM users_old;
        DROP TABLE users_old;
        PRAGMA foreign_keys = ON;
        """
    )


def repair_stale_user_foreign_keys(connection: sqlite3.Connection) -> None:
    enrollments_sql = get_table_sql(connection, "enrollments")
    payments_sql = get_table_sql(connection, "payments")
    if "users_old" not in enrollments_sql and "users_old" not in payments_sql:
        return
    connection.executescript(
        """
        PRAGMA foreign_keys = OFF;

        ALTER TABLE enrollments RENAME TO enrollments_old;
        CREATE TABLE enrollments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            class_batch_id INTEGER NOT NULL REFERENCES class_batches(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN ('pending_payment', 'confirmed', 'completed', 'cancelled')),
            approval_status TEXT NOT NULL CHECK(approval_status IN ('pending', 'approved', 'rejected')),
            payment_status TEXT NOT NULL CHECK(payment_status IN ('pending', 'paid', 'failed', 'refunded')),
            emergency_contact_name TEXT NOT NULL,
            emergency_contact_phone TEXT NOT NULL,
            notes TEXT,
            certificate_eligible INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, class_batch_id)
        );
        INSERT INTO enrollments (
            id, user_id, class_batch_id, status, approval_status, payment_status,
            emergency_contact_name, emergency_contact_phone, notes, certificate_eligible, created_at
        )
        SELECT
            id, user_id, class_batch_id, status, approval_status, payment_status,
            emergency_contact_name, emergency_contact_phone, notes,
            COALESCE(certificate_eligible, 0), created_at
        FROM enrollments_old;
        DROP TABLE enrollments_old;

        ALTER TABLE payments RENAME TO payments_old;
        CREATE TABLE payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enrollment_id INTEGER NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            payment_method TEXT NOT NULL DEFAULT 'gcash',
            provider_reference TEXT NOT NULL UNIQUE,
            amount_cents INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            status TEXT NOT NULL CHECK(status IN ('pending', 'succeeded', 'failed')),
            receipt_url TEXT,
            approved_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            approved_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO payments (
            id, enrollment_id, provider, payment_method, provider_reference, amount_cents,
            currency, status, receipt_url, approved_by_user_id, approved_at, created_at
        )
        SELECT
            id, enrollment_id, provider, COALESCE(payment_method, 'gcash'), provider_reference, amount_cents,
            currency, status, receipt_url, approved_by_user_id, approved_at, created_at
        FROM payments_old;
        DROP TABLE payments_old;

        PRAGMA foreign_keys = ON;
        """
    )
    connection.commit()


def ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}
    if column_name not in columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def get_table_sql(connection: sqlite3.Connection, table_name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row["sql"] if row and row["sql"] else ""
