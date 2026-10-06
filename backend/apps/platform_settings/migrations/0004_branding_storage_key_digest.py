"""Use fixed-width digest uniqueness for long object-storage keys."""

from __future__ import annotations

import hashlib

from django.db import migrations, models
from django.db.models import Count


def _digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


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
    duplicate = (
        BrandingAsset.objects.values("storage_key")
        .annotate(row_count=Count("pk"))
        .filter(row_count__gt=1)
        .exists()
    )
    if duplicate:
        raise RuntimeError(
            "Duplicate branding storage keys exist; resolve them before applying the digest "
            "migration. No assets were deleted or changed."
        )

    batch = []
    for row in BrandingAsset.objects.only("pk", "storage_key").iterator(chunk_size=1000):
        row.storage_key_hash = _digest(row.storage_key)
        batch.append(row)
        if len(batch) >= 500:
            BrandingAsset.objects.bulk_update(batch, ["storage_key_hash"], batch_size=500)
            batch.clear()
    if batch:
        BrandingAsset.objects.bulk_update(batch, ["storage_key_hash"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("platform_settings", "0003_platformconfiguration_allow_image_messages_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="brandingasset",
            name="storage_key_hash",
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.RunPython(backfill_storage_key_hashes, migrations.RunPython.noop),
        migrations.RunPython(_drop_legacy_storage_key_unique_index),
        migrations.AlterField(
            model_name="brandingasset",
            name="storage_key_hash",
            field=models.CharField(editable=False, max_length=64),
        ),
        migrations.AddConstraint(
            model_name="brandingasset",
            constraint=models.UniqueConstraint(
                fields=("storage_key_hash",), name="unique_branding_storage_key_hash"
            ),
        ),
    ]
