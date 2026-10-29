from django.core.management.base import BaseCommand

from core import services


class Command(BaseCommand):
    help = (
        "Check each project's Git status (clean, uncommitted, unpushed, ...) and store "
        "it on the Project record. Read-only against every project's checkout -- never "
        "writes to a repo, only to this app's database -- so it is safe to run against "
        "the real instance. Run by hand; nothing triggers this automatically."
    )

    def handle(self, *args, **options):
        for result in services.sync_git_status():
            marker = "*" if result["changed"] else " "
            suffix = f" ({result['git_detail']})" if result["git_detail"] else ""
            self.stdout.write(f"{marker} {result['folder']}: {result['git_status_display']}{suffix}")
