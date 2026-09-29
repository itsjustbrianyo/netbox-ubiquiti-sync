"""
Change-logging support for writes made outside a web/API request.

NetBox records ObjectChange (changelog) entries from model signals, but only
while a request context is active.  The web UI and REST API set one up
automatically; management commands and background jobs do not, which is why
ORM writes from `manage.py sync_unifi` never appeared in the changelog.

`change_logging()` recreates what NetBox's own `runscript` command does: it
builds a fake request tied to a service user and enters NetBox's
`event_tracking` context.  Every create/update/delete (and M2M change such as
tags or tagged VLANs) made inside the block is then logged normally, with
before/after diffs, attributed to that user, and grouped under one request ID
so a whole sync run can be viewed together in the changelog.

The service user is configurable via the plugin setting `changelog_username`
(default "unifi-sync").  If it doesn't exist it is created as an inactive
account with an unusable password — it can't log in, it only owns changes.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager, nullcontext

log = logging.getLogger(__name__)

DEFAULT_USERNAME = "unifi-sync"


def get_sync_user(username: str = DEFAULT_USERNAME):
    """Get or create the service account that sync changes are attributed to."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user, created = User.objects.get_or_create(
        username=username,
        defaults={"is_active": False},
    )
    if created:
        user.set_unusable_password()
        user.save()
        log.info("Created changelog service user %r", username)
    return user


def _fake_request(user):
    """Build a NetBoxFakeRequest, handling its location across 4.x releases."""
    try:
        from utilities.request import NetBoxFakeRequest
    except ImportError:  # older NetBox
        from utilities.utils import NetBoxFakeRequest

    return NetBoxFakeRequest({
        "META": {},
        "COOKIES": {},
        "POST": {},
        "GET": {},
        "FILES": {},
        "user": user,
        "path": "",
        "id": uuid.uuid4(),
    })


@contextmanager
def change_logging(username: str = DEFAULT_USERNAME, enabled: bool = True):
    """
    Context manager: ORM writes inside the block are recorded in the
    NetBox changelog.  Yields the request (or None when disabled / on
    failure, in which case the sync still runs, just without logging).
    """
    if not enabled:
        with nullcontext():
            yield None
        return

    try:
        from netbox.context_managers import event_tracking
        user = get_sync_user(username)
        request = _fake_request(user)
        ctx = event_tracking(request)
    except Exception as exc:  # never block a sync over logging
        log.warning("Changelog context unavailable, changes won't be logged: %s", exc)
        with nullcontext():
            yield None
        return

    with ctx:
        yield request
