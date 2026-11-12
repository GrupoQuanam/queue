from datetime import date, datetime
from unittest.mock import patch

from psycopg2 import OperationalError

from odoo.tests import tagged

from ..delay import GQDelayable
from ..exception import GQFailedJobError, GQNothingToDoJob
from .common import GQCommon


class FakeSerializationFailure(OperationalError):
    """Error de concurrencia de PostgreSQL (serialization_failure)."""

    pgcode = "40001"
    pgerror = "could not serialize access due to concurrent update"


class FakeDiskFull(OperationalError):
    """OperationalError que no es de concurrencia (disk_full)."""

    pgcode = "53100"
    pgerror = "could not extend file"


@tagged("post_install", "-at_install")
class TestGQJobEdge(GQCommon):
    """Ramas poco frecuentes: serializacion, errores de BD y camino con commit."""

    # ── Serializacion de argumentos ───────────────────────────────────────────

    def test_date_arguments_round_trip(self):
        moment = datetime(2026, 5, 15, 10, 30)
        day = date(2026, 5, 15)
        job = self.partner.with_gq_delay().gq_probe(
            moment, day, {"__gqtype__": "otro", "value": 1}, when=day
        )
        self.assertIn('"__gqtype__": "datetime"', job.args)
        self._run()
        self.assertEqual(job.state, "done", job.exc_info)
        call = self.calls[0]
        self.assertEqual(
            call["args"], (moment, day, {"__gqtype__": "otro", "value": 1})
        )
        self.assertEqual(call["kwargs"], {"when": day})

    def test_unserializable_argument(self):
        with self.assertRaises(TypeError):
            self.partner.with_gq_delay().gq_probe(object())

    def test_special_names_are_not_enqueued(self):
        delayable = GQDelayable(self.partner)
        with self.assertRaises(AttributeError):
            delayable.__deepcopy__  # noqa: B018
        self.assertFalse(self.Job.search([("method_name", "=", "__deepcopy__")]))

    def test_model_level_function(self):
        """Sin registros, la funcion se ejecuta sobre el modelo vacio."""
        job = self.env["res.partner"].with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "done", job.exc_info)
        self.assertEqual(self.calls[0]["ids"], [])

    # ── Excepciones de control ────────────────────────────────────────────────

    def test_nothing_to_do(self):
        self.side_effect = GQNothingToDoJob("ya procesado")
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.result, "ya procesado")

    def test_nothing_to_do_without_message(self):
        self.side_effect = GQNothingToDoJob()
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "done")
        self.assertTrue(job.result)

    def test_failed_job_error(self):
        self.side_effect = GQFailedJobError("faltan datos")
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "failed")
        self.assertIn("faltan datos", job.exc_info)
        self.assertEqual(job.retry_count, 1, "No se reintenta")

    def test_set_failed_without_traceback(self):
        job = self.partner.with_gq_delay().gq_probe()
        job._gq_set_failed()
        self.assertEqual(job.state, "failed")
        self.assertFalse(job.exc_info)

    def test_postpone_without_result(self):
        job = self.partner.with_gq_delay().gq_probe()
        job._gq_set_started()
        job._gq_postpone_pending(seconds=60)
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.retry_count, 1)
        self.assertFalse(job.result)
        self.assertTrue(job.eta)

    # ── Errores de PostgreSQL ─────────────────────────────────────────────────

    def test_concurrency_error_is_retried_for_free(self):
        self.side_effect = FakeSerializationFailure()
        job = self.partner.with_gq_delay(max_retries=1).gq_probe()
        job._gq_process()
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.retry_count, 0, "El intento no se cuenta")
        self.assertIn("could not serialize", job.result)
        self.assertTrue(job.eta)

    def test_other_operational_error_fails(self):
        self.side_effect = FakeDiskFull()
        job = self.partner.with_gq_delay().gq_probe()
        job._gq_process()
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.retry_count, 1)

    # ── Camino con commit ─────────────────────────────────────────────────────

    def test_runner_commits_started_before_running(self):
        """'started' y el intento se confirman antes de ejecutar la funcion."""
        job = self.partner.with_gq_delay().gq_probe()
        commits = []

        def commit():
            self.env.flush_all()
            self.env.cr.execute(
                "SELECT state, retry_count FROM gq_job WHERE id = %s", (job.id,)
            )
            commits.append((self.env.cr.fetchone(), len(self.calls)))

        with patch.object(self.env.cr, "commit", commit):
            self.Job._gq_job_runner()
        self.assertEqual(job.state, "done", job.exc_info)
        self.assertEqual(
            commits,
            [(("started", 1), 0), (("done", 1), 1)],
            "Primer commit antes de ejecutar, segundo al terminar",
        )

    def test_job_taken_by_another_runner_is_skipped(self):
        job = self.partner.with_gq_delay().gq_probe()
        with (
            patch.object(self.env.cr, "commit", lambda: None),
            patch.object(type(self.Job), "_gq_lock_started", lambda self: False),
        ):
            job._gq_process(commit=True)
        self.assertFalse(self.calls, "La funcion no se ejecuta sin el bloqueo")
        self.assertEqual(job.state, "started")

    def test_lock_started(self):
        job = self.partner.with_gq_delay().gq_probe()
        self.assertFalse(job._gq_lock_started(), "Solo bloquea jobs en 'started'")
        job._gq_set_started()
        self.env.flush_all()
        self.assertTrue(job._gq_lock_started())

    # ── Huerfanos ─────────────────────────────────────────────────────────────

    def test_no_orphan_check_scheduled_in_the_past(self):
        self._make_started(minutes_ago=5, attempts=1)
        triggers = self.env["ir.cron.trigger"]
        before = triggers.search_count([])
        self.Job._gq_schedule_orphan_check(grace=1)
        self.assertEqual(triggers.search_count([]), before)

    def test_no_orphan_check_without_started_jobs(self):
        self.Job.search([("state", "=", "started")]).write({"state": "done"})
        triggers = self.env["ir.cron.trigger"]
        before = triggers.search_count([])
        self.Job._gq_schedule_orphan_check(grace=120)
        self.assertEqual(triggers.search_count([]), before)

    # ── Grupo ─────────────────────────────────────────────────────────────────

    def test_group_progress(self):
        partners = self._partners(4)
        jobs = partners.with_gq_delay(chunk_size=2).gq_probe()
        self.assertEqual(jobs[0].group_job_count, 2)
        self.assertEqual(jobs[0].group_done_count, 0)
        jobs[0]._gq_process()
        jobs.invalidate_recordset(["group_job_count", "group_done_count"])
        self.assertEqual(jobs[1].group_done_count, 1)
        single = self.partner.with_gq_delay().gq_probe()
        self.assertEqual(single.group_job_count, 0)

    def test_view_group_action(self):
        job = self.partner.with_gq_delay(group="g").gq_probe()
        action = job.action_view_group()
        self.assertEqual(action["res_model"], "gq.job")
        self.assertEqual(self.Job.search(action["domain"]), job)

    def test_failure_in_group_without_pending_jobs(self):
        self.side_effect = ValueError("boom")
        job = self.partner.with_gq_delay(group="solo").gq_probe()
        job._gq_process()
        self.assertEqual(job.state, "failed")

    def test_dependencies_from_different_groups(self):
        a = self.partner.with_gq_delay(group="g1").gq_probe()
        b = self.partner.with_gq_delay(group="g2").gq_probe()
        job = self.partner.with_gq_delay(depends_on=a | b).gq_probe()
        self.assertFalse(job.group_uuid, "No se une a ningun grupo")
        self.assertEqual(job.state, "waiting")
        self.assertEqual(job.depends_on_ids, a | b)

    # ── Limpieza ──────────────────────────────────────────────────────────────

    def test_gc_with_nothing_to_delete(self):
        job = self.partner.with_gq_delay().gq_probe()
        self.Job._gc_finished_jobs()
        self.assertTrue(job.exists())
