# SQLite and Alembic

The default database URL is `sqlite:///./var/jobcockpit.db`. All schema changes use Alembic. SQLite batch migrations are enabled in `apps/api/alembic/env.py` so table-altering migrations remain portable.

Avoid PostgreSQL-only features in MVP migrations. Use text IDs, portable JSON columns, and SQLAlchemy models as the schema source.

