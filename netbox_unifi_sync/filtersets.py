import django_filters

from .choices import SyncStatusChoices
from .models import SyncLog


class SyncLogFilterSet(django_filters.FilterSet):
    q = django_filters.CharFilter(method="search", label="Search")
    status = django_filters.ChoiceFilter(choices=SyncStatusChoices)

    class Meta:
        model  = SyncLog
        fields = ["unifi_site_id", "status"]

    def search(self, queryset, name, value):
        return queryset.filter(
            unifi_site_id__icontains=value
        ) | queryset.filter(
            unifi_site_name__icontains=value
        ) | queryset.filter(
            site_name__icontains=value
        )
