"""
Shared helper for creating the custom fields this plugin owns.

NetBox 4.x attaches custom fields to ``core.ObjectType`` rows (a proxy of
Django's ContentType), so those are looked up here rather than via
``ContentType.objects.get_for_model``.
"""

import logging

log = logging.getLogger(__name__)


def ensure_custom_field(
    app_label: str,
    model: str,
    name: str,
    label: str,
    description: str = "",
    cf_type: str = "text",
) -> None:
    """Create a custom field on <app_label>.<model> if it does not exist."""
    try:
        from core.models import ObjectType
        from extras.models import CustomField

        object_type = ObjectType.objects.get(app_label=app_label, model=model)
        cf, _created = CustomField.objects.get_or_create(
            name=name,
            defaults={
                "label": label,
                "description": description,
                "type": cf_type,
                "required": False,
                "ui_editable": "yes",
            },
        )
        if not cf.object_types.filter(pk=object_type.pk).exists():
            cf.object_types.add(object_type)
    except Exception as exc:
        log.warning("Could not ensure custom field %s: %s", name, exc)
