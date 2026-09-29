from utilities.choices import ChoiceSet


class SyncStatusChoices(ChoiceSet):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED  = "failed"

    CHOICES = [
        (PENDING, "Pending",  "secondary"),
        (RUNNING, "Running",  "warning"),
        (SUCCESS, "Success",  "success"),
        (FAILED,  "Failed",   "danger"),
    ]
