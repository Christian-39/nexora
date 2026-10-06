"""Replace unsupported partial/long-column uniqueness with portable keys."""

from __future__ import annotations

import hashlib

from django.db import migrations, models
from django.db.models import Count, Q


def _digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _backfill_storage_key_hashes(apps, schema_editor):
    Attachment = apps.get_model("conversations", "Attachment")
    duplicate = (
        Attachment.objects.values("storage_key")
        .annotate(row_count=Count("pk"))
        .filter(row_count__gt=1)
        .first()
    )
    if duplicate:
        raise RuntimeError(
            "Duplicate Attachment.storage_key values exist; resolve them before applying "
            "the fixed-width uniqueness migration. No storage keys were changed."
        )

    batch = []
    for row in Attachment.objects.only("pk", "storage_key").iterator(chunk_size=1000):
        row.storage_key_hash = _digest(row.storage_key)
        batch.append(row)
        if len(batch) >= 500:
            Attachment.objects.bulk_update(batch, ["storage_key_hash"], batch_size=500)
            batch.clear()
    if batch:
        Attachment.objects.bulk_update(batch, ["storage_key_hash"], batch_size=500)


def _drop_legacy_long_unique_index(model, field_name, schema_editor):
    """Remove the legacy long-column index from databases that created it.

    Older deployed versions of these migrations declared 512/1000-character
    fields unique. A fresh database now starts without that unsupported index;
    an upgraded database may still have one, so inspect and drop only the exact
    single-column unique index when present. MariaDB reports vendor ``mysql``.
    """
    if schema_editor.connection.vendor != "mysql":
        return
    column = model._meta.get_field(field_name).column
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(cursor, model._meta.db_table)
    for name, details in constraints.items():
        if details.get("unique") and details.get("columns") == [column]:
            sql = schema_editor._delete_unique_sql(model, name)
            if sql:
                schema_editor.execute(sql)


def _drop_attachment_storage_key_unique(apps, schema_editor):
    Attachment = apps.get_model("conversations", "Attachment")
    _drop_legacy_long_unique_index(Attachment, "storage_key", schema_editor)


def _check_private_conversation_duplicates(apps, schema_editor):
    Conversation = apps.get_model("conversations", "Conversation")
    duplicate = (
        Conversation.objects.filter(kind="ADMIN_PRIVATE")
        .values("admin_id", "member_id")
        .annotate(row_count=Count("pk"))
        .filter(row_count__gt=1)
        .first()
    )
    if duplicate:
        raise RuntimeError(
            "Duplicate administrator/member private conversations exist. Merge or archive "
            "duplicates before applying the portable unique index; this migration will not "
            "delete or merge messages."
        )


def _drop_legacy_partial_index(apps, schema_editor):
    """Drop the old partial index where it exists; MySQL never created it."""
    if not schema_editor.connection.features.supports_partial_indexes:
        return
    Conversation = apps.get_model("conversations", "Conversation")
    constraint = models.UniqueConstraint(
        fields=["admin", "member"],
        condition=Q(kind="ADMIN_PRIVATE"),
        name="unique_admin_member_private",
    )
    schema_editor.remove_constraint(Conversation, constraint)


class Migration(migrations.Migration):
    dependencies = [
        ("conversations", "0006_messagereceipt_receipt_recipient_unread_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="attachment",
            name="storage_key_hash",
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.RunPython(_backfill_storage_key_hashes, migrations.RunPython.noop),
        migrations.RunPython(_drop_attachment_storage_key_unique),
        migrations.AlterField(
            model_name="attachment",
            name="storage_key_hash",
            field=models.CharField(editable=False, max_length=64),
        ),
        migrations.AddConstraint(
            model_name="attachment",
            constraint=models.UniqueConstraint(
                fields=("storage_key_hash",), name="unique_attachment_storage_key_hash"
            ),
        ),
        migrations.RunPython(_check_private_conversation_duplicates, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunPython(_drop_legacy_partial_index, migrations.RunPython.noop)],
            state_operations=[
                migrations.RemoveConstraint(
                    model_name="conversation", name="unique_admin_member_private"
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(
                fields=("kind", "admin", "member"), name="unique_admin_member_private"
            ),
        ),
    ]
