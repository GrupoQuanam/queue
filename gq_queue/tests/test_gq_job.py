from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import config

from ..exception import GQRetryableJobError


@tagged("post_install", "-at_install")
class TestGQJob(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, login="gq_user", groups="base.group_user")
        cls.partner = cls.env["res.partner"].create({"name": "GQ"})
        cls.Job = cls.env["gq.job"]

    def _patch_probe(self, side_effect=None):
        """Agrega a res.partner una funcion de prueba que registra su entorno."""
        calls = []

        def gq_probe(records, *args, **kwargs):
            calls.append(
                {
                    "uid": records.env.uid,
                    "su": records.env.su,
                    "context": dict(records.env.context),
                    "ids": records.ids,
                    "args": args,
                }
            )
            if side_effect:
                raise side_effect

        patcher = patch.object(
            type(self.env["res.partner"]), "gq_probe", gq_probe, create=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def _run(self):
        self.Job._gq_job_runner(commit=False)

    # ── Encolado ──────────────────────────────────────────────────────────────

    def test_enqueue_as_internal_user(self):
        """Un usuario sin permisos sobre gq.job puede encolar."""
        partner = self.partner.with_user(self.user).with_context(lang="en_US", foo=1)
        job = partner.with_gq_delay().gq_probe(5)
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.user_id, self.user)
        self.assertEqual(job.company_id, self.user.company_id)
        self.assertFalse(job.run_sudo)
        self.assertIn('"lang": "en_US"', job.context)
        self.assertNotIn("foo", job.context)

    def test_internal_user_cannot_read_jobs(self):
        job = self.partner.with_gq_delay().gq_probe()
        self.assertFalse(
            self.Job.with_user(self.user).has_access("read"),
            "gq.job no debe ser legible por usuarios internos",
        )
        self.assertTrue(job.exists())

    # ── Entorno de ejecucion ──────────────────────────────────────────────────

    def test_run_with_enqueuing_user_and_context(self):
        calls = self._patch_probe()
        partner = self.partner.with_user(self.user).with_context(lang="en_US", foo=1)
        job = partner.with_gq_delay().gq_probe(5)
        self._run()
        self.assertEqual(job.state, "done", job.exc_info)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["uid"], self.user.id)
        self.assertFalse(calls[0]["su"])
        self.assertEqual(calls[0]["context"].get("lang"), "en_US")
        self.assertNotIn("foo", calls[0]["context"])
        self.assertEqual(calls[0]["ids"], self.partner.ids)
        self.assertEqual(calls[0]["args"], (5,))

    def test_run_keeps_sudo(self):
        calls = self._patch_probe()
        job = self.partner.with_user(self.user).sudo().with_gq_delay().gq_probe()
        self.assertTrue(job.run_sudo)
        self._run()
        self.assertEqual(job.state, "done", job.exc_info)
        self.assertEqual(calls[0]["uid"], self.user.id)
        self.assertTrue(calls[0]["su"])

    # ── Intentos ──────────────────────────────────────────────────────────────

    def test_attempt_counted_at_start(self):
        self._patch_probe()
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.retry_count, 1)

    def test_retryable_until_max_retries(self):
        self._patch_probe(side_effect=GQRetryableJobError("later", seconds=30))
        job = self.partner.with_gq_delay(max_retries=1).gq_probe()
        self._run()
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.retry_count, 1)
        self.assertGreater(job.eta, fields.Datetime.now())
        job.eta = False
        self._run()
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.retry_count, 2)

    def test_unexpected_error_fails_without_breaking_runner(self):
        self._patch_probe(side_effect=ValueError("boom"))
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "failed")
        self.assertIn("boom", job.exc_info)

    # ── Huerfanos ─────────────────────────────────────────────────────────────

    def _make_started(self, minutes_ago, attempts, max_retries=5):
        job = self.partner.with_gq_delay(max_retries=max_retries).gq_probe()
        job.write(
            {
                "state": "started",
                "date_started": fields.Datetime.now() - timedelta(minutes=minutes_ago),
                "retry_count": attempts,
            }
        )
        return job

    def test_recover_orphans(self):
        retry = self._make_started(minutes_ago=5, attempts=1)
        exhausted = self._make_started(minutes_ago=5, attempts=6)
        unlimited = self._make_started(minutes_ago=5, attempts=5, max_retries=0)
        unlimited_dead = self._make_started(minutes_ago=5, attempts=6, max_retries=0)
        alive = self._make_started(minutes_ago=0, attempts=1)
        with patch.object(type(self.Job), "_gq_time_limit", lambda self: 120):
            self.Job._gq_recover_started_jobs()
        self.assertEqual(retry.state, "pending")
        self.assertEqual(retry.retry_count, 1)
        self.assertEqual(exhausted.state, "failed")
        self.assertTrue(exhausted.exc_info)
        self.assertEqual(unlimited.state, "pending")
        self.assertEqual(unlimited_dead.state, "failed")
        self.assertEqual(
            alive.state, "started", "Un job mas nuevo que el limite puede seguir vivo"
        )

    def test_recover_schedules_check_for_recent_started(self):
        self._make_started(minutes_ago=0, attempts=1)
        triggers = self.env["ir.cron.trigger"]
        before = triggers.search([])
        with patch.object(type(self.Job), "_gq_time_limit", lambda self: 120):
            self.Job._gq_recover_started_jobs()
        new = triggers.search([]) - before
        self.assertTrue(new)
        self.assertGreater(new[0].call_at, fields.Datetime.now())

    # ── Presupuesto de tiempo ─────────────────────────────────────────────────

    def test_time_limit_from_config(self):
        with patch.dict(
            config.options, {"limit_time_real_cron": -1, "limit_time_real": 120}
        ):
            self.assertEqual(self.Job._gq_time_limit(), 120)
        with patch.dict(
            config.options, {"limit_time_real_cron": 900, "limit_time_real": 120}
        ):
            self.assertEqual(self.Job._gq_time_limit(), 900)
        with patch.dict(
            config.options, {"limit_time_real_cron": 0, "limit_time_real": 120}
        ):
            self.assertEqual(self.Job._gq_time_limit(), 0)

    def test_runner_stops_at_budget_and_retriggers(self):
        calls = self._patch_probe()
        self.partner.with_gq_delay().gq_probe(1)
        second = self.partner.with_gq_delay().gq_probe(2)
        self.Job._gq_job_runner(commit=False, max_seconds=0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second.state, "pending")
