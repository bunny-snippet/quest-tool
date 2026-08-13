"""Safely import real respondent emails into the dedicated Cint vault pool."""

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from prescreener_vault.cint_email_pool import add_real_email, email_pool_status


class Command(BaseCommand):
    help = (
        "Add real respondent emails to the encrypted Cint pool or display pool counts. "
        "Raw addresses are never printed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--add",
            action="append",
            default=[],
            metavar="EMAIL",
            help="Add one real email. Repeat the option to add several addresses.",
        )
        parser.add_argument(
            "--file",
            type=Path,
            help="UTF-8 text/CSV file; the first non-empty column on each row is used.",
        )
        parser.add_argument(
            "--status",
            action="store_true",
            help="Display counts without revealing addresses.",
        )

    @staticmethod
    def _file_values(path):
        if not path.is_file():
            raise CommandError(f"Email file does not exist: {path}")
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                for row in csv.reader(stream):
                    value = next((cell.strip() for cell in row if cell.strip()), "")
                    if value and value.lower() not in {"email", "email address"}:
                        yield value
        except UnicodeError as exc:
            raise CommandError("Email file must use UTF-8 encoding.") from exc

    def handle(self, *args, **options):
        supplied = list(options["add"])
        if options.get("file"):
            supplied.extend(self._file_values(options["file"]))
        if not supplied and not options["status"]:
            raise CommandError("Use --add, --file, or --status.")

        added = existing = invalid = 0
        invalid_rows = []
        for position, email in enumerate(supplied, start=1):
            try:
                _, created = add_real_email(email)
            except ValueError:
                invalid += 1
                invalid_rows.append(str(position))
            else:
                if created:
                    added += 1
                else:
                    existing += 1

        if supplied:
            self.stdout.write(
                f"processed={len(supplied)} added={added} existing={existing} invalid={invalid}"
            )
            if invalid_rows:
                self.stderr.write(
                    "Invalid addresses at input positions: " + ", ".join(invalid_rows)
                )

        if options["status"] or supplied:
            status = email_pool_status()
            self.stdout.write(
                " ".join(f"{key}={value}" for key, value in status.items())
            )

        if invalid:
            raise CommandError(
                "Some addresses were invalid; valid real addresses were imported safely."
            )
