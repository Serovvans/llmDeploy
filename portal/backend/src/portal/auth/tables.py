"""Таблицы входа (docs/portal-api.md §9.1–9.4)."""

import sqlalchemy as sa

from portal.core.db import metadata

users = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("login", sa.Text, nullable=False, unique=True),
    sa.Column("full_name", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("password_hash", sa.Text, nullable=False),
    sa.Column("must_change_password", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("is_blocked", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("totp_secret", sa.LargeBinary),
    sa.Column("totp_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("totp_last_step", sa.BigInteger),
    sa.Column("last_login_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("role IN ('admin', 'employee')", name="users_role_check"),
)

backup_codes = sa.Table(
    "backup_codes",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "user_id",
        sa.Uuid,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    sa.Column("code_hmac", sa.LargeBinary, nullable=False),
    sa.Column("used_at", sa.DateTime(timezone=True)),
)

sessions = sa.Table(
    "sessions",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "user_id",
        sa.Uuid,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    sa.Column("token_hash", sa.LargeBinary, nullable=False, unique=True),
    sa.Column("second_factor_passed", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
)

auth_throttle = sa.Table(
    "auth_throttle",
    metadata,
    sa.Column("scope", sa.Text, primary_key=True),
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("failures", sa.Integer, nullable=False),
    sa.Column("first_failure_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("locked_until", sa.DateTime(timezone=True)),
    sa.CheckConstraint("scope IN ('login', 'ip')", name="auth_throttle_scope_check"),
)
