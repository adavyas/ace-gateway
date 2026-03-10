# Alembic Migrations

This repo historically used `Base.metadata.create_all()` at startup.

To move toward production-safe schema changes, Alembic is introduced for
Postgres deployments. The API can still run locally with SQLite using
`create_all()` for convenience, but production should prefer:

```bash
cd ace
alembic upgrade head
```

Notes:
- `DATABASE_URL` is read from `scope_config.py` / environment variables.
- Migration autogeneration should be reviewed carefully (especially around
  JSON columns and pgvector types).

