"""Stop orphan LinTO catch-up captures, delete their segments and the state of
ended recordings (fork): the pass Celery Beat runs every
LINTO_CATCHUP_RECONCILE_INTERVAL_S, by hand or from a CronJob.
"""

from django.core.management.base import BaseCommand

from core.services import linto_catchup


class Command(BaseCommand):
    """Run one reconciliation pass."""

    help = (
        "Stop catch-up captures whose recording is not active; delete orphan "
        "segments and the state (and Studio conversation) of ended recordings."
    )

    def handle(self, *args, **options):
        if not linto_catchup.is_enabled():
            self.stdout.write("LinTO catch-up disabled")
            return
        result = linto_catchup.reconcile()
        self.stdout.write(
            f"stopped={result['stopped']} deleted_prefixes={result['deleted']} "
            f"discarded_states={result['discarded']}"
        )
