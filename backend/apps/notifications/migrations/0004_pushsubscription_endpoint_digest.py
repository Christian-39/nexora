"""Use a fixed-width digest for globally unique push endpoints on MySQL/MariaDB."""

from __future__ import annotations

import hashlib

from django.db import migrations, models
from django.db.models import Count


def _digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _ensure_endpoint_hash_column(apps, schema_editor):
    PushSubscription = apps.get_model("notifications", "PushSubscription")
    field = models.CharField(editable=False, max_length=64, null=True)
    field.set_attributes_from_name("endpoint_hash")
    with schema_editor.connection.cursor() as cursor:
        columns = schema_editor.connection.introspection.get_table_description(
            cursor, PushSubscription._meta.db_table
        )
    if field.column not in {column.name for column in columns}:
        schema_editor.add_field(PushSubscription, field)


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
    batch = []
    for row in PushSubscription.objects.only("pk", "endpoint").iterator(chunk_size=1000):
        row.endpoint_hash = _digest(row.endpoint)
        batch.append(row)
        if len(batch) >= 500:
            PushSubscription.objects.bulk_update(batch, ["endpoint_hash"], batch_size=500)
            batch.clear()
    if batch:
        PushSubscription.objects.bulk_update(batch, ["endpoint_hash"], batch_size=500)

    # SHA-256 output is canonical lowercase ASCII, so this remains an exact
    # duplicate check even on MySQL's usual case-insensitive text collation.
    has_duplicate_hashes = (
        PushSubscription.objects.values("endpoint_hash")
        .annotate(row_count=Count("pk"))
        .filter(row_count__gt=1)
        .exists()
    )
    if has_duplicate_hashes:
        raise RuntimeError(
            "Duplicate push endpoints (or a digest collision) exist; resolve them before applying "
            "the endpoint digest migration. No subscriptions were deleted or reassigned."
        )


def _add_endpoint_hash_unique(apps, schema_editor):
    PushSubscription = apps.get_model("notifications", "PushSubscription")
    constraint = models.UniqueConstraint(
        fields=["endpoint_hash"], name="unique_push_subscription_endpoint_hash"
    )
    with schema_editor.connection.cursor() as cursor:
        current = schema_editor.connection.introspection.get_constraints(cursor, PushSubscription._meta.db_table)
    existing = current.get(constraint.name)
    expected_columns = [PushSubscription._meta.get_field(name).column for name in constraint.fields]
    if existing:
        if existing.get("unique") and existing.get("columns") == expected_columns:
            return
        raise RuntimeError(
            f"Constraint {constraint.name} exists with an unexpected definition; inspect the database."
        )
    schema_editor.add_constraint(PushSubscription, constraint)


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0003_notification_aggregate_count_and_more"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(_ensure_endpoint_hash_column)],
            state_operations=[
                migrations.AddField(
                    model_name="pushsubscription",
                    name="endpoint_hash",
                    field=models.CharField(editable=False, max_length=64, null=True),
                ),
            ],
        ),
        migrations.RunPython(backfill_endpoint_hashes, migrations.RunPython.noop),
        migrations.RunPython(_drop_legacy_endpoint_unique_index),
        migrations.AlterField(
            model_name="pushsubscription",
            name="endpoint_hash",
            field=models.CharField(editable=False, max_length=64),
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(_add_endpoint_hash_unique)],
            state_operations=[
                migrations.AddConstraint(
                    model_name="pushsubscription",
                    constraint=models.UniqueConstraint(
                        fields=("endpoint_hash",), name="unique_push_subscription_endpoint_hash"
                    ),
                ),
            ],
        ),
    ]
