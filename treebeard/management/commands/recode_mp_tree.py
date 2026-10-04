"""
Maintenance command to change the alphabet/steplen of a Materialized Path
tree without changing primary keys or business fields.

Run while the model still uses the *old* encoding::

    python manage.py recode_mp_tree myapp.MyNode \
        --old-alphabet 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ --old-steplen 4 \
        --new-alphabet 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz \
        --new-steplen 4

Use ``--check`` for a read-only pre-flight, ``--rollback`` to undo a previous
migration deterministically, and ``--resume`` to continue an interrupted one.
"""

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from treebeard.recode import (
    RecodeError,
    forget_recode,
    plan_recode,
    recode_mp_paths,
    recode_status,
    resume_recode,
    rollback_recode,
)


class Command(BaseCommand):
    help = "Re-encode Materialized Path tree paths with new alphabet/steplen"

    def add_arguments(self, parser):
        parser.add_argument("model", help="Dotted model label, e.g. catalog.Category")
        parser.add_argument("--old-alphabet")
        parser.add_argument("--old-steplen", type=int)
        parser.add_argument("--new-alphabet")
        parser.add_argument("--new-steplen", type=int)
        parser.add_argument("--database", default=None, help="Database alias to use")
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--check", action="store_true", help="Read-only validation, no writes")
        parser.add_argument("--resume", action="store_true", help="Resume an interrupted migration")
        parser.add_argument("--rollback", action="store_true", help="Undo a migration")
        parser.add_argument("--forget", action="store_true", help="Drop migration metadata after success")
        parser.add_argument(
            "--repair-fields",
            action="store_true",
            help="Recompute stale depth/numchild instead of aborting (orphans still abort)",
        )

    def handle(self, *args, **options):
        try:
            model = apps.get_model(options["model"])
        except LookupError as exc:
            raise CommandError(f"unknown model {options['model']!r}") from exc

        using = options["database"]

        if options["resume"]:
            result = resume_recode(model, using=using, batch_size=options["batch_size"])
            self.stdout.write(self.style.SUCCESS(f"resumed recoding of {result.node_count} node(s)"))
            return
        if options["rollback"]:
            count = rollback_recode(model, using=using, batch_size=options["batch_size"])
            self.stdout.write(self.style.SUCCESS(f"rolled back {count} node(s) to the old paths"))
            return
        if options["forget"]:
            forget_recode(model, using=using)
            self.stdout.write(self.style.SUCCESS("migration metadata removed"))
            return

        missing = [
            name
            for name in ("old_alphabet", "old_steplen", "new_alphabet", "new_steplen")
            if options[name] is None
        ]
        if missing:
            raise CommandError(f"missing required arguments: {', '.join('--' + n.replace('_', '-') for n in missing)}")

        if options["check"]:
            plan = plan_recode(
                model,
                options["old_alphabet"],
                options["old_steplen"],
                options["new_alphabet"],
                options["new_steplen"],
                repair_fields=options["repair_fields"],
                using=using,
            )
            self._report_plan(plan)
            if not plan.ok:
                for problem in plan.problems:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  {problem['category']}: "
                            f"pk={problem.get('pk')} detail={problem.get('detail', '')}"
                        )
                    )
                raise SystemExit(1)
            self.stdout.write(self.style.SUCCESS("OK: migration can proceed"))
            return

        try:
            result = recode_mp_paths(
                model,
                options["old_alphabet"],
                options["old_steplen"],
                options["new_alphabet"],
                options["new_steplen"],
                using=using,
                batch_size=options["batch_size"],
                repair_fields=options["repair_fields"],
            )
        except RecodeError as exc:
            problems = getattr(exc, "problems", None)
            if problems:
                for problem in problems:
                    self.stdout.write(
                        self.style.ERROR(
                            f"  {problem['category']}: "
                            f"pk={problem.get('pk')} detail={problem.get('detail', '')}"
                        )
                    )
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"recoded {result.node_count} node(s); now deploy the new alphabet/steplen "
                "on the model class"
            )
        )
        status = recode_status(model, using=using)
        self.stdout.write(f"journal phase: {status['phase'] if status else 'none'}")

    def _report_plan(self, plan):
        self.stdout.write(f"model:          {plan.model_label}")
        self.stdout.write(f"nodes:          {plan.node_count}")
        self.stdout.write(f"new capacity:   {plan.capacity_per_level} siblings per level")
        self.stdout.write(f"max siblings:   {plan.max_siblings}")
        self.stdout.write(
            f"max new path:   {plan.max_path_length} chars (column holds {plan.path_max_length})"
        )
        self.stdout.write(f"marker:         {plan.marker!r}")
        if plan.problems:
            self.stdout.write(self.style.ERROR(f"problems:       {len(plan.problems)}"))
        else:
            self.stdout.write("problems:       none")
