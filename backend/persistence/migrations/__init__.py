"""Forward-only ``PRAGMA user_version`` migrations (one module per version).

Versions up to 18 live in ``Database._migrate`` itself; this package holds
the later steps, applied in version order by ``Database._migrate``:

    19  acquisition_migration.migrate_acquisition_anchors
    20  inventory.migrate
    21  no_acquisition.migrate
    22  origin_resolutions.migrate
    23  unresolved_count.migrate

Historical migrations remain unchanged (project rule). Each step sets its own
``user_version``; after the chain ``Database._migrate`` requires
``user_version == _SCHEMA_VERSION`` and fails closed otherwise.
"""
