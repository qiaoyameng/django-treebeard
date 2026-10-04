"""Maintenance-window command to re-encode a Materialized Path tree."""

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from treebeard.exceptions import PathOverflow
from treebeard.mp_recode import RecodeError, check_encoding, recode_tree
from treebeard.mp_tree import MP_Node


class Command(BaseCommand):
    help = (
        "Rewrite the path column of an MP_Node model from an old "
        "alphabet/steplen encoding to a new one, without changing primary "
        "keys or business fields. Run this during a maintenance window, "
        "BEFORE switching the model class to the new alphabet/steplen. "
        "The whole rewrite runs in one transaction; on failure nothing is "
        "changed and the command can simply be re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument("model", help="Model to migrate, as app_label.ModelName")
        parser.add_argument("--old-alphabet", required=True, help="Alphabet the stored paths are encoded with")
        parser.add_argument("--old-steplen", required=True, type=int, help="Step length the stored paths use")
        parser.add_argument(
            "--new-alphabet",
            help="Alphabet to re-encode paths with (not required for --check)",
        )
        parser.add_argument("--new-steplen", type=int, help="Step length to re-encode paths with")
        parser.add_argument(
            "--check",
            action="store_true",
            help="Only validate the data against the --old-* parameters and report problems; "
            "after a migration, re-run with the new values as --old-* to verify the result. "
            "This check is repeatable and writes nothing.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Compute and print the migration plan without writing anything",
        )

    def handle(self, *args, **options):
        model = apps.get_model(options["model"])
        if model is None or not issubclass(model, MP_Node):
            raise CommandError(f"{options['model']} is not an MP_Node model")

        old_alphabet = options["old_alphabet"]
        old_steplen = options["old_steplen"]

        if options["check"]:
            problems = check_encoding(model, old_alphabet, old_steplen)
            problems = {key: value for key, value in problems.items() if value}
            if problems:
                for kind, pks in problems.items():
                    self.stderr.write(f"{kind}: {pks}")
                raise CommandError("encoding check FAILED, see the problem list above")
            self.stdout.write(self.style.SUCCESS("encoding check passed"))
            return

        if not options["new_alphabet"] or not options["new_steplen"]:
            raise CommandError("--new-alphabet and --new-steplen are required unless --check is used")

        try:
            plan = recode_tree(
                model,
                old_alphabet=old_alphabet,
                old_steplen=old_steplen,
                new_alphabet=options["new_alphabet"],
                new_steplen=options["new_steplen"],
                dry_run=options["dry_run"],
            )
        except RecodeError as exc:
            for kind, pks in exc.problems.items():
                self.stderr.write(f"{kind}: {pks}")
            raise CommandError(str(exc)) from exc
        except PathOverflow as exc:
            raise CommandError(str(exc)) from exc

        mode = "planned (dry run)" if options["dry_run"] else "migrated"
        self.stdout.write(self.style.SUCCESS(f"{mode}: {plan.summary()}"))
        if not options["dry_run"]:
            self.stdout.write(
                "Now update the model class to the new alphabet/steplen and "
                "deploy. Old code must not run against the new paths."
            )
