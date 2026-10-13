"""kolom role pada users (RBAC)

Revision ID: 0002_user_role
Revises: 0001_auth_bookmarks
Create Date: 2026-10-13

Menambahkan ``users.role`` (``user`` default / ``admin``) untuk kontrol akses
berbasis peran. Master admin ditetapkan lewat env ``ADMIN_EMAILS``; kolom ini
adalah sumber kebenaran role di database.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_user_role"
down_revision = "0001_auth_bookmarks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("role", sa.String(length=16), nullable=False, server_default="user"),
    )
    op.create_index("ix_users_role", "users", ["role"])


def downgrade() -> None:
    op.drop_index("ix_users_role", table_name="users")
    op.drop_column("users", "role")
