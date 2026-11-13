from datetime import date, datetime
from unittest.mock import patch

from psycopg2 import OperationalError
from psycopg2.errors import SerializationFailure

from odoo import fields
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

    def test_job_function_can_commit(self):
        """La funcion del job puede hacer commit en el cursor del runner."""
        commits = []
        self.during = lambda records: records.env.cr.commit()
        job = self.partner.with_gq_delay().gq_probe()
        with patch.object(self.env.cr, "commit", lambda: commits.append(job.state)):
            self.Job._gq_job_runner()
        self.assertEqual(job.state, "done", job.exc_info)
        self.assertEqual(
            commits,
            ["started", "started", "done"],
            "Commit de 'started', commit de la funcion y commit final",
        )

    def test_failure_in_commit_mode_rolls_back(self):
        """En corrida real, un fallo revierte lo que la funcion no confirmo."""
        events = []
        self.side_effect = ValueError("boom")
        job = self.partner.with_gq_delay().gq_probe()
        with (
            patch.object(self.env.cr, "commit", lambda: events.append("commit")),
            patch.object(self.env.cr, "rollback", lambda: events.append("rollback")),
        ):
            job._gq_process(commit=True)
        self.assertEqual(job.state, "failed")
        self.assertEqual(events, ["commit", "rollback", "commit"])

    # ── Conflictos entre runners ──────────────────────────────────────────────

    def _fake_transactions(self):
        """Corrida real sin confirmar de verdad: commit y rollback solo se cuentan."""
        events = []
        for name in ("commit", "rollback"):
            patcher = patch.object(
                self.env.cr, name, lambda name=name: events.append(name)
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        return events

    def _flaky(self, function_name, failures):
        """Hace que una funcion de gq.job choque con otro runner ``failures`` veces."""
        job_cls = type(self.Job)
        real = getattr(job_cls, function_name)
        calls = []

        def flaky(records, *args, **kwargs):
            calls.append(1)
            if len(calls) <= failures:
                raise SerializationFailure("could not serialize access")
            return real(records, *args, **kwargs)

        patcher = patch.object(job_cls, function_name, flaky)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_runner_retries_when_another_runner_takes_the_job(self):
        events = self._fake_transactions()
        self._flaky("_gq_acquire_one_job", failures=2)
        job = self.partner.with_gq_delay().gq_probe()
        self.Job._gq_job_runner()
        self.assertEqual(job.state, "done", job.exc_info)
        self.assertEqual(events.count("rollback"), 2, "Un rollback por conflicto")

    def test_conflict_is_not_retried_without_commit(self):
        """Sin commit no se puede revertir sin perder trabajo: el error sube."""
        self._flaky("_gq_acquire_one_job", failures=1)
        self.partner.with_gq_delay().gq_probe()
        with self.assertRaises(SerializationFailure):
            self._run()

    def test_runner_leaves_work_to_next_run_after_many_conflicts(self):
        self._fake_transactions()
        self._flaky("_gq_acquire_one_job", failures=1000)
        job = self.partner.with_gq_delay().gq_probe()
        with patch.object(type(self.Job), "_gq_cron_trigger") as trigger:
            self.Job._gq_job_runner()
        self.assertEqual(job.state, "pending")
        self.assertFalse(self.calls)
        trigger.assert_called_once_with()

    def test_job_outcome_retried_on_conflict(self):
        events = self._fake_transactions()
        self._flaky("_gq_stop_group", failures=1)
        self.side_effect = ValueError("boom")
        job = self.partner.with_gq_delay(group="g").gq_probe()
        job._gq_process(commit=True)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.exc_name, "ValueError")
        self.assertEqual(events.count("rollback"), 2, "Fallo del job y conflicto")

    def test_job_outcome_not_applied_after_many_conflicts(self):
        self._fake_transactions()
        self._flaky("_gq_stop_group", failures=1000)
        self.side_effect = ValueError("boom")
        job = self.partner.with_gq_delay(group="g").gq_probe()
        with self.assertLogs("odoo.addons.gq_queue.models.gq_job", "WARNING") as logs:
            job._gq_process(commit=True)
        self.assertTrue(
            any("no se pudo registrar su resultado" in line for line in logs.output)
        )

    # ── Disparo del cron ──────────────────────────────────────────────────────

    def _immediate_triggers(self):
        cron = self.env.ref("gq_queue.gq_job_cron")
        return self.env["ir.cron.trigger"].search(
            [("cron_id", "=", cron.id), ("call_at", "<=", fields.Datetime.now())]
        )

    def test_one_immediate_trigger_per_transaction(self):
        before = self._immediate_triggers()
        for index in range(5):
            self.partner.with_gq_delay().gq_probe(index)
        self.assertEqual(
            len(self._immediate_triggers() - before),
            1,
            "Encolar varios jobs no repite el disparo inmediato",
        )

    def test_trigger_recreated_after_savepoint_rollback(self):
        before = self._immediate_triggers()
        with self.assertRaises(ZeroDivisionError), self.env.cr.savepoint():
            self.partner.with_gq_delay().gq_probe("revertido")
            self.assertEqual(len(self._immediate_triggers() - before), 1)
            1 / 0  # noqa: B018
        self.assertFalse(self._immediate_triggers() - before, "El disparo se revirtio")
        self.partner.with_gq_delay().gq_probe("ok")
        self.assertEqual(len(self._immediate_triggers() - before), 1)

    # ── Bloqueo de vida ───────────────────────────────────────────────────────

    def test_alive_lock_held_while_running(self):
        seen = []
        job = self.partner.with_gq_delay().gq_probe()
        self.assertTrue(self._alive_lock_is_free(job))
        self.during = lambda records: seen.append(self._alive_lock_is_free(job))
        self._run()
        self.assertEqual(seen, [False], "Durante la ejecucion el job esta marcado vivo")
        self.assertTrue(self._alive_lock_is_free(job), "Al terminar se libera")

    def test_alive_lock_released_on_failure(self):
        self.side_effect = ValueError("boom")
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "failed")
        self.assertTrue(self._alive_lock_is_free(job))

    def test_job_in_use_by_another_session_is_postponed(self):
        job = self.partner.with_gq_delay().gq_probe()
        other = self.partner.with_gq_delay()._gq_probe()
        self._hold_alive_lock(job)
        with self.assertLogs("odoo.addons.gq_queue.models.gq_job", "WARNING"):
            self._run()
        self.assertEqual(len(self.calls), 1, "Solo se ejecuto el otro job")
        self.assertEqual(other.state, "done")
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.retry_count, 0, "No se cuenta el intento")
        self.assertGreater(job.eta, fields.Datetime.now())

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
