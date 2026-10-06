"""Use fixed-width digest uniqueness for long object-storage keys."""

from __future__ import annotations

import hashlib

from django.db import migrations, models
from django.db.models import Count


def _digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _ensure_storage_key_hash_column(apps, schema_editor):
    BrandingAsset = apps.get_model("platform_settings", "BrandingAsset")
    field = models.CharField(editable=False, max_length=64, null=True)
    field.set_attributes_from_name("storage_key_hash")
    with schema_editor.connection.cursor() as cursor:
        columns = schema_editor.connection.introspection.get_table_description(
            cursor, BrandingAsset._meta.db_table
        )
    if field.column not in {column.name for column in columns}:
        schema_editor.add_field(BrandingAsset, field)


def _drop_legacy_storage_key_unique_index(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    BrandingAsset = apps.get_model("platform_settings", "BrandingAsset")
    column = BrandingAsset._meta.get_field("storage_key").column
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(
            cursor, BrandingAsset._meta.db_table
        )
    for name, details in constraints.items():
        if details.get("unique") and details.get("columns") == [column]:
            sql = schema_editor._delete_unique_sql(BrandingAsset, name)
            if sql:
                schema_editor.execute(sql)


def backfill_storage_key_hashes(apps, schema_editor):
    BrandingAsset = apps.get_model("platform_settings", "BrandingAsset")
    batch = []
    for row in BrandingAsset.objects.only("pk", "storage_key").iterator(chunk_size=1000):
        row.storage_key_hash = _digest(row.storage_key)
        batch.append(row)
        if len(batch) >= 500:
            BrandingAsset.objects.bulk_update(batch, ["storage_key_hash"], batch_size=500)
            batch.clear()
    if batch:
        BrandingAsset.objects.bulk_update(batch, ["storage_key_hash"], batch_size=500)

    # Compare canonical lowercase hashes, not the original case-sensitive
    # object keys under MySQL's commonly case-insensitive default collation.
    has_duplicate_hashes = (
        BrandingAsset.objects.values("storage_key_hash")
        .annotate(row_count=Count("pk"))
        .filter(row_count__gt=1)
        .exists()
    )
    if has_duplicate_hashes:
        raise RuntimeError(
            "Duplicate branding storage keys (or a digest collision) exist; resolve them before "
            "applying the digest migration. No assets were deleted or changed."
        )


def _add_storage_key_hash_unique(apps, schema_editor):
    BrandingAsset = apps.get_model("platform_settings", "BrandingAsset")
    constraint = models.UniqueConstraint(
        fields=["storage_key_hash"], name="unique_branding_storage_key_hash"
    )
    with schema_editor.connection.cursor() as cursor:
        current = schema_editor.connection.introspection.get_constraints(cursor, BrandingAsset._meta.db_table)
    existing = current.get(constraint.name)
    expected_columns = [BrandingAsset._meta.get_field(name).column for name in constraint.fields]
    if existing:
        if existing.get("unique") and existing.get("columns") == expected_columns:
            return
        raise RuntimeError(
            f"Constraint {constraint.name} exists with an unexpected definition; inspect the database."
        )
    schema_editor.add_constraint(BrandingAsset, constraint)


class Migration(migrations.Migration):
    dependencies = [
        ("platform_settings", "0003_platformconfiguration_allow_image_messages_and_more"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(_ensure_storage_key_hash_column)],
            state_operations=[
                migrations.AddField(
                    model_name="brandingasset",
                    name="storage_key_hash",
                    field=models.CharField(editable=False, max_length=64, null=True),
                ),
            ],
        ),
        migrations.RunPython(backfill_storage_key_hashes, migrations.RunPython.noop),
        migrations.RunPython(_drop_legacy_storage_key_unique_index),
        migrations.AlterField(
            model_name="brandingasset",
            name="storage_key_hash",
            field=models.CharField(editable=False, max_length=64),
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(_add_storage_key_hash_unique)],
            state_operations=[
                migrations.AddConstraint(
                    model_name="brandingasset",
                    constraint=models.UniqueConstraint(
                        fields=("storage_key_hash",), name="unique_branding_storage_key_hash"
                    ),
                ),
            ],
        ),
    ]
