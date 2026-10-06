"""Use a fixed-width digest for globally unique push endpoints on MySQL/MariaDB."""

from __future__ import annotations

import hashlib

from django.db import migrations, models
from django.db.models import Count


def _digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _drop_legacy_endpoint_unique_index(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    PushSubscription = apps.get_model("notifications", "PushSubscription")
    column = PushSubscription._meta.get_field("endpoint").column
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(
            cursor, PushSubscription._meta.db_table
        )
    for name, details in constraints.items():
        if details.get("unique") and details.get("columns") == [column]:
            sql = schema_editor._delete_unique_sql(PushSubscription, name)
            if sql:
                schema_editor.execute(sql)


def backfill_endpoint_hashes(apps, schema_editor):
    PushSubscription = apps.get_model("notifications", "PushSubscription")
    duplicate = (
        PushSubscription.objects.values("endpoint")
        .annotate(row_count=Count("pk"))
        .filter(row_count__gt=1)
        .first()
    )
    if duplicate:
        raise RuntimeError(
            "Duplicate push endpoints exist; resolve them before applying the endpoint digest "
            "migration. No subscriptions were deleted or reassigned."
        )

    batch = []
    for row in PushSubscription.objects.only("pk", "endpoint").iterator(chunk_size=1000):
        row.endpoint_hash = _digest(row.endpoint)
        batch.append(row)
        if len(batch) >= 500:
            PushSubscription.objects.bulk_update(batch, ["endpoint_hash"], batch_size=500)
            batch.clear()
    if batch:
        PushSubscription.objects.bulk_update(batch, ["endpoint_hash"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0003_notification_aggregate_count_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="pushsubscription",
            name="endpoint_hash",
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.RunPython(backfill_endpoint_hashes, migrations.RunPython.noop),
        migrations.RunPython(_drop_legacy_endpoint_unique_index),
        migrations.AlterField(
            model_name="pushsubscription",
            name="endpoint_hash",
            field=models.CharField(editable=False, max_length=64),
        ),
        migrations.AddConstraint(
            model_name="pushsubscription",
            constraint=models.UniqueConstraint(
                fields=("endpoint_hash",), name="unique_push_subscription_endpoint_hash"
            ),
        ),
    ]
