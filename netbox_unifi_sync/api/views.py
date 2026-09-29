from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.viewsets import GenericViewSet

from ..models import SyncLog
from .serializers import SyncLogSerializer


class SyncLogViewSet(RetrieveModelMixin, ListModelMixin, GenericViewSet):
    """
    Read-only API endpoint for UniFi sync log records.
    Sync jobs are triggered via the management command, not the API.
    """
    queryset         = SyncLog.objects.all()
    serializer_class = SyncLogSerializer
