import os
from datetime import timedelta
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from ..exception import GQRetryableJobError
from .common import GQCommon


@tagged("post_install", "-at_install")
class TestGQJobFunction(GQCommon):
    """Configuracion por funcion, reintentos, on_fail y datos de ejecucion."""

    # ── Configuracion por funcion ─────────────────────────────────────────────

    def test_function_name(self):
        function = self._configure()
        self.assertEqual(function.name, "<res.partner>.gq_probe")

    def test_function_is_unique_per_model(self):
        self._configure()
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            self._configure()

    def test_function_must_exist(self):
        with self.assertRaises(ValidationError):
            self._configure(method="gq_no_such_function")
        with self.assertRaises(ValidationError):
            self._configure(on_fail_method="gq_no_such_function")

    def test_config_cache_is_invalidated(self):
        config = self.env["gq.job.function"].job_config
        self.assertEqual(config("res.partner", "gq_probe")["retry_pattern"], {})
        function = self._configure(retry_pattern="{1: 10}")
        self.assertEqual(config("res.partner", "gq_probe")["retry_pattern"], {1: 10})
        function.retry_pattern = "{1: 20}"
        self.assertEqual(
            config("res.partner", "gq_probe")["retry_pattern"],
            {1: 20},
            "Cambiar la configuracion invalida la cache",
        )
        function.unlink()
        self.assertEqual(config("res.partner", "gq_probe")["retry_pattern"], {})

    # ── Patron de reintentos ──────────────────────────────────────────────────

    def test_retry_pattern_parsing(self):
        parse = self.env["gq.job.function"]._parse_retry_pattern
        self.assertEqual(parse(False), {})
        self.assertEqual(parse("  "), {})
        self.assertEqual(parse("{1: 10, 5: 60}"), {1: 10, 5: 60})
        self.assertEqual(parse("{1: (1, 10), 5: [30, 60]}"), {1: (1, 10), 5: (30, 60)})

    def test_retry_pattern_validation(self):
        invalid = [
            "{1: 10",  # sintaxis
            "nombre",  # no es un literal
            "[10, 20]",  # no es diccionario
            "{0: 10}",  # intento menor que 1
            "{'a': 10}",  # intento no entero
            "{1: -1}",  # espera negativa
            "{1: 'x'}",  # espera no entera
            "{1: (5, 1)}",  # rango invertido
            "{1: (1, 2, 3)}",  # rango de 3 valores
            "{1: (1, 'x')}",  # rango no entero
        ]
        for pattern in invalid:
            with self.subTest(pattern=pattern), self.assertRaises(ValidationError):
                self._configure(retry_pattern=pattern).unlink()

    def _retry_seconds(self, attempts, err=None):
        job = self.partner.with_gq_delay().gq_probe()
        job.retry_count = attempts
        return job._gq_retry_seconds(err or GQRetryableJobError("x"))

    def test_retry_seconds_default(self):
        self.assertEqual(self._retry_seconds(1), 5)

    def test_retry_seconds_from_pattern(self):
        self._configure(retry_pattern="{2: 10, 4: 60}")
        self.assertEqual(self._retry_seconds(1), 5, "Antes de la primera entrada")
        self.assertEqual(self._retry_seconds(2), 10)
        self.assertEqual(self._retry_seconds(3), 10)
        self.assertEqual(self._retry_seconds(4), 60)
        self.assertEqual(self._retry_seconds(9), 60)

    def test_retry_seconds_from_error(self):
        self._configure(retry_pattern="{1: 10}")
        err = GQRetryableJobError("x", seconds=120)
        self.assertEqual(self._retry_seconds(1, err), 120)

    def test_retry_seconds_random_range(self):
        self._configure(retry_pattern="{1: (30, 40)}")
        for _attempt in range(20):
            self.assertIn(self._retry_seconds(1), range(30, 41))

    def test_runner_applies_retry_pattern(self):
        self._configure(retry_pattern="{1: 300}")
        self.side_effect = GQRetryableJobError("ocupado")
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "pending")
        self.assertGreater(job.eta, fields.Datetime.now() + timedelta(seconds=290))

    def test_ignore_retry(self):
        self.side_effect = GQRetryableJobError("espera", ignore_retry=True)
        job = self.partner.with_gq_delay(max_retries=1).gq_probe()
        for _attempt in range(3):
            job._gq_process()
            self.assertEqual(job.state, "pending")
            self.assertEqual(job.retry_count, 0, "El reintento no consume intentos")

    # ── on_fail ───────────────────────────────────────────────────────────────

    def test_on_fail_called_on_final_failure(self):
        self._configure(on_fail_method="gq_on_fail")
        self.side_effect = ValueError("boom")
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "failed")
        self.assertEqual(len(self.fail_calls), 1)
        call = self.fail_calls[0]
        self.assertEqual(call["ids"], self.partner.ids)
        self.assertEqual(call["exc_name"], "ValueError")
        self.assertEqual(call["exc_message"], "boom")
        self.assertIn("Traceback", call["exc_info"])

    def test_on_fail_not_called_on_success_or_retry(self):
        self._configure(on_fail_method="gq_on_fail")
        self.partner.with_gq_delay().gq_probe()
        self._run()
        self.side_effect = GQRetryableJobError("later")
        self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertFalse(self.fail_calls)

    def test_on_fail_after_retries_exhausted(self):
        self._configure(on_fail_method="gq_on_fail")
        self.side_effect = GQRetryableJobError("later")
        job = self.partner.with_gq_delay(max_retries=1).gq_probe()
        job._gq_process()
        self.assertFalse(self.fail_calls)
        job._gq_process()
        self.assertEqual(job.state, "failed")
        self.assertEqual(self.fail_calls[0]["exc_name"], "GQRetryableJobError")

    def test_on_fail_error_does_not_break_runner(self):
        self._configure(on_fail_method="gq_on_fail")
        self.side_effect = ValueError("boom")
        self.on_fail_error = RuntimeError("on_fail roto")
        job = self.partner.with_gq_delay().gq_probe()
        other = self.partner.with_gq_delay()._gq_probe()
        with self.assertLogs("odoo.addons.gq_queue.models.gq_job", "ERROR") as logs:
            self._run()
        self.assertEqual(job.state, "failed")
        self.assertEqual(other.state, "failed", "El runner siguio con el siguiente job")
        self.assertTrue(any("error en on_fail" in line for line in logs.output))

    def test_on_fail_for_dead_job(self):
        self._configure(on_fail_method="gq_on_fail")
        job = self._make_started(minutes_ago=5, attempts=6)
        with patch.object(type(self.Job), "_gq_time_limit", lambda self: 120):
            self.Job._gq_recover_started_jobs()
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.exc_name, "GQDeadJobError")
        self.assertEqual(self.fail_calls[0]["exc_name"], "GQDeadJobError")

    def test_recover_without_dead_jobs(self):
        self._configure(on_fail_method="gq_on_fail")
        job = self._make_started(minutes_ago=5, attempts=1)
        with patch.object(type(self.Job), "_gq_time_limit", lambda self: 120):
            self.Job._gq_recover_started_jobs()
        self.assertEqual(job.state, "pending")
        self.assertFalse(job.exc_name)
        self.assertFalse(self.fail_calls)

    # ── Datos de ejecucion ────────────────────────────────────────────────────

    def test_execution_data_on_success(self):
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.worker_pid, os.getpid())
        self.assertGreaterEqual(job.exec_time, 0)
        self.assertFalse(job.exc_name)

    def test_execution_data_on_failure(self):
        self.side_effect = ValueError("boom")
        job = self.partner.with_gq_delay().gq_probe()
        job._gq_set_started()
        job.date_started = fields.Datetime.now() - timedelta(seconds=30)
        with mute_logger("odoo.addons.gq_queue.models.gq_job"):
            try:
                raise ValueError("boom")
            except ValueError as err:
                job._gq_set_failed(exc_info="tb", exc=err)
        self.assertEqual(job.exc_name, "ValueError")
        self.assertEqual(job.exc_message, "boom")
        self.assertGreaterEqual(job.exec_time, 30)

    def test_requeue_clears_execution_data(self):
        self.side_effect = ValueError("boom")
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertTrue(job.exc_name)
        job.action_requeue()
        self.assertFalse(job.exc_name)
        self.assertFalse(job.exc_message)
        self.assertFalse(job.worker_pid)
        self.assertFalse(job.exec_time)

    def test_cancel_sets_date_cancelled(self):
        job = self.partner.with_gq_delay().gq_probe()
        job.action_cancel()
        self.assertTrue(job.date_cancelled)
        job.action_requeue()
        self.assertFalse(job.date_cancelled)

    def test_job_uuid_in_context(self):
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(self.calls[0]["context"]["job_uuid"], job.uuid)

    # ── Recordsets como argumentos ────────────────────────────────────────────

    def test_recordset_arguments(self):
        others = self._partners(2)
        job = self.partner.with_user(self.user).with_gq_delay().gq_probe(
            others.with_user(self.user), company=self.env.company.sudo()
        )
        self._run()
        self.assertEqual(job.state, "done", job.exc_info)
        call = self.calls[0]
        records = call["args"][0]
        self.assertEqual(records, others)
        self.assertEqual(records.env.uid, self.user.id, "Usa el entorno del job")
        self.assertFalse(records.env.su)
        company = call["kwargs"]["company"]
        self.assertEqual(company, self.env.company)
        self.assertTrue(company.env.su, "Conserva el modo sudo del argumento")

    # ── Integracion con ir.cron ───────────────────────────────────────────────

    def test_cron_action_runs_the_queue(self):
        """La accion del cron ejecuta el runner con commit, como en produccion."""
        cron = self.env.ref("gq_queue.gq_job_cron")
        self.assertEqual(cron.code.strip(), "model._gq_job_runner()")
        self._configure(on_fail_method="gq_on_fail", retry_pattern="{1: 10}")
        ok = self.partner.with_gq_delay().gq_probe(self.partner)
        bad_partner = self._partners(1)
        bad = bad_partner.with_gq_delay().gq_probe()
        self.fail_ids = set(bad_partner.ids)
        with patch.object(self.env.cr, "commit", self.env.flush_all):
            cron.method_direct_trigger()
        self.assertEqual(ok.state, "done", ok.exc_info)
        self.assertEqual(bad.state, "failed")
        self.assertEqual(len(self.fail_calls), 1)
        progress = self.env["ir.cron.progress"].search(
            [("cron_id", "=", cron.id)], order="id desc", limit=1
        )
        self.assertEqual((progress.done, progress.remaining), (2, 0))

    def test_runner_does_not_trigger_itself_for_released_jobs(self):
        """Liberar un job en espera no dispara otra pasada del cron: el runner
        lo procesa en la misma corrida. Solo se programa una ETA futura."""
        first = self.partner.with_gq_delay().gq_probe("first")
        ready = self.partner.with_gq_delay(depends_on=first).gq_probe("ready")
        later = self.partner.with_gq_delay(
            depends_on=first, eta=fields.Datetime.now() + timedelta(hours=1)
        ).gq_probe("later")
        past = self.partner.with_gq_delay(
            depends_on=first, eta=fields.Datetime.now() - timedelta(hours=1)
        ).gq_probe("past")
        triggers = self.env["ir.cron.trigger"]
        before = triggers.search([])
        self._run()
        self.assertEqual((ready.state, past.state), ("done", "done"))
        self.assertEqual(later.state, "pending")
        new = triggers.search([]) - before
        self.assertEqual(new.mapped("call_at"), [later.eta])
