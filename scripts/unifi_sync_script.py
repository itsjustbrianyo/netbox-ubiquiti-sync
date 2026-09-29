"""
NetBox custom script: run the UniFi sync from the web UI.

Upload via Customization → Scripts → Add.  Runs the same code as
`manage.py sync_unifi`, on NetBox's background worker (netbox-rq).
Can be run on demand or scheduled to recur from the script's run page.
"""

import io

from django.core.management import call_command
from django.core.management.base import CommandError

from dcim.models import Site
from extras.scripts import BooleanVar, ObjectVar, Script


class UnifiSync(Script):

    class Meta:
        name = "UniFi Sync"
        description = "Sync UniFi devices, IPAM and SSIDs into NetBox"
        commit_default = True
        # A full sync across many sites can take a while
        job_timeout = 3600

    site = ObjectVar(
        model=Site,
        required=False,
        query_params={"cf_unifi_site_id__empty": "false"},
        description="Sync just this site. Leave blank to sync all mapped sites.",
    )
    dry_run = BooleanVar(
        default=False,
        description="Collect from UniFi without writing anything to NetBox",
    )
    backfill_device_types = BooleanVar(
        default=False,
        description=(
            "Instead of syncing, enrich existing Ubiquiti device types from the "
            "NetBox Community Device Type Library and exit. Only fills device "
            "types with no data yet (no rack height, image, or comments) — "
            "never overwrites anything. Requires enable_devicetype_library."
        ),
    )

    def run(self, data, commit):
        if data["backfill_device_types"]:
            out, err = io.StringIO(), io.StringIO()
            try:
                call_command("sync_unifi", "--backfill-device-types", stdout=out, stderr=err)
            except (CommandError, SystemExit) as exc:
                self.log_failure(f"Backfill failed: {exc}")
                return
            for line in out.getvalue().splitlines():
                if line.strip():
                    self.log_info(line)
            for line in err.getvalue().splitlines():
                if line.strip():
                    self.log_warning(line)
            return

        options = {"dry_run": data["dry_run"]}

        if not commit:
            self.log_warning(
                "'Commit changes' is unticked, so NetBox will roll back "
                "everything this run writes. Tick it on the run page (and "
                "re-create any schedule with it ticked) to keep the changes."
            )

        site = data.get("site")
        if site:
            unifi_site = site.custom_field_data.get("unifi_site_id")
            if not unifi_site:
                self.log_failure(f"{site} has no UniFi Site ID set.")
                return
            options["site"] = unifi_site

        # Attribute changelog entries to whoever ran the script. Scheduled
        # runs are attributed to the user who scheduled them.
        user = getattr(getattr(self, "request", None), "user", None)
        if user and user.is_authenticated:
            options["user"] = user.username

        out, err = io.StringIO(), io.StringIO()
        try:
            call_command("sync_unifi", stdout=out, stderr=err, **options)
        except (CommandError, SystemExit) as exc:
            self.log_failure(f"Sync failed: {exc}")
        finally:
            for line in out.getvalue().splitlines():
                if line.strip():
                    self.log_info(line)
            for line in err.getvalue().splitlines():
                if line.strip():
                    self.log_warning(line)

        if options["dry_run"]:
            self.log_info("Dry run: no changes were written.")
        elif not commit:
            self.log_warning("Sync ran, but its changes will be rolled back (Commit changes is off).")
        else:
            self.log_success("UniFi sync complete.")
