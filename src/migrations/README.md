# Migrations

`src/schema.sql` is the authoritative schema and is applied in full to a **new**
database. Once a database exists in the field, structural changes arrive here as
numbered files, applied in ascending order by `src.db._apply_migrations`.

Rules:

1. Edit `src/schema.sql` first so a fresh database and a migrated one converge.
2. Add `NNN_short_description.sql` here, where `NNN` is the new
   `src.db.SCHEMA_VERSION`, and bump that constant.
3. Migrations are additive where possible. Never drop a column that an existing
   sign-off trail or citation depends on.

`001` is intentionally absent: version 1 is what `schema.sql` creates.
