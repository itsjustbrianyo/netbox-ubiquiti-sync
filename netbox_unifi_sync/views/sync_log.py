from netbox.views import generic

from ..filtersets import SyncLogFilterSet
from ..forms import SyncLogFilterForm
from ..models import SyncLog
from ..tables import SyncLogTable


class SyncLogListView(generic.ObjectListView):
    queryset       = SyncLog.objects.all()
    table          = SyncLogTable
    filterset      = SyncLogFilterSet
    filterset_form = SyncLogFilterForm
    template_name  = "netbox_unifi_sync/synclog_list.html"
    actions        = ()   # read-only: no add / import / export / bulk actions


class SyncLogView(generic.ObjectView):
    queryset      = SyncLog.objects.all()
    template_name = "netbox_unifi_sync/synclog.html"
