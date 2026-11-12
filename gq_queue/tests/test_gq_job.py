from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
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

    def setUp(self):
        """Agrega a res.partner funciones de prueba que registran su entorno."""
        super().setUp()
        self.calls = []
        self.side_effect = None
        self.fail_ids = set()
        test = self

        def gq_probe(records, *args, **kwargs):
            test.calls.append(
                {
                    "uid": records.env.uid,
                    "su": records.env.su,
                    "context": dict(records.env.context),
                    "ids": records.ids,
                    "args": args,
                    "kwargs": kwargs,
                }
            )
            if test.side_effect:
                raise test.side_effect
            if test.fail_ids & set(records.ids):
                raise ValueError("lote con error")

        partner_cls = type(self.env["res.partner"])
        for name in ("gq_probe", "_gq_probe"):
            patcher = patch.object(partner_cls, name, gq_probe, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

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

    def test_enqueue_private_function(self):
        job = self.partner.with_gq_delay()._gq_probe(1)
        self._run()
        self.assertEqual(job.state, "done", job.exc_info)
        self.assertEqual(len(self.calls), 1)

    def test_enqueue_unknown_function(self):
        with self.assertRaises(AttributeError):
            self.partner.with_gq_delay().gq_no_such_function()
        with self.assertRaises(AttributeError):
            # un campo no es una funcion
            self.partner.with_gq_delay().name()

    def test_chunk_size(self):
        partners = self.env["res.partner"].create(
            [{"name": f"GQ {i}"} for i in range(5)]
        )
        jobs = partners.with_gq_delay(chunk_size=2).gq_probe("x")
        self.assertEqual(len(jobs), 3)
        self._run()
        self.assertEqual(set(jobs.mapped("state")), {"done"})
        self.assertEqual(
            [call["ids"] for call in self.calls],
            [partners[:2].ids, partners[2:4].ids, partners[4:].ids],
        )
        self.assertTrue(jobs[0].name.endswith("[1/3]"))

    def test_chunk_size_not_needed(self):
        jobs = self.partner.with_gq_delay(chunk_size=10).gq_probe()
        self.assertEqual(len(jobs), 1)

    def test_identity_key(self):
        first = self.partner.with_gq_delay(identity_key="sync-1").gq_probe()
        again = self.partner.with_gq_delay(identity_key="sync-1").gq_probe()
        other = self.partner.with_gq_delay(identity_key="sync-2").gq_probe()
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self._run()
        self.assertEqual(first.state, "done")
        after = self.partner.with_gq_delay(identity_key="sync-1").gq_probe()
        self.assertNotEqual(first, after, "Un job terminado no bloquea la clave")

    def test_identity_key_with_chunk_size(self):
        with self.assertRaises(ValueError):
            self.partner.with_gq_delay(identity_key="k", chunk_size=2)

    # ── Entorno de ejecucion ──────────────────────────────────────────────────

    def test_run_with_enqueuing_user_and_context(self):
        partner = self.partner.with_user(self.user).with_context(lang="en_US", foo=1)
        job = partner.with_gq_delay().gq_probe(5)
        self._run()
        self.assertEqual(job.state, "done", job.exc_info)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["uid"], self.user.id)
        self.assertFalse(call["su"])
        self.assertEqual(call["context"].get("lang"), "en_US")
        self.assertNotIn("foo", call["context"])
        self.assertEqual(call["ids"], self.partner.ids)
        self.assertEqual(call["args"], (5,))

    def test_run_keeps_sudo(self):
        job = self.partner.with_user(self.user).sudo().with_gq_delay().gq_probe()
        self.assertTrue(job.run_sudo)
        self._run()
        self.assertEqual(job.state, "done", job.exc_info)
        self.assertEqual(self.calls[0]["uid"], self.user.id)
        self.assertTrue(self.calls[0]["su"])

    # ── Intentos ──────────────────────────────────────────────────────────────

    def test_attempt_counted_at_start(self):
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.retry_count, 1)

    def test_retryable_until_max_retries(self):
        self.side_effect = GQRetryableJobError("later", seconds=30)
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
        self.side_effect = ValueError("boom")
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
        self.partner.with_gq_delay().gq_probe(1)
        second = self.partner.with_gq_delay().gq_probe(2)
        self.Job._gq_job_runner(commit=False, max_seconds=0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(second.state, "pending")

    def test_runner_notifies_cron_progress(self):
        cron = self.env.ref("gq_queue.gq_job_cron")
        cron, progress = cron._add_progress()
        self.partner.with_gq_delay().gq_probe(1)
        self.partner.with_gq_delay().gq_probe(2)
        self.Job.with_context(**cron.env.context)._gq_job_runner(commit=False)
        self.assertEqual(progress.done, 2)
        self.assertEqual(
            progress.remaining, 0, "remaining > 0 relanzaria el runner sin presupuesto"
        )

    # ── Acciones de usuario ───────────────────────────────────────────────────

    def test_requeue_failed_job(self):
        self.side_effect = ValueError("boom")
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "failed")
        self.side_effect = None
        job.action_requeue()
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.retry_count, 0)
        self.assertFalse(job.exc_info)
        self._run()
        self.assertEqual(job.state, "done")

    def test_requeue_pending_job_is_rejected(self):
        job = self.partner.with_gq_delay().gq_probe()
        with self.assertRaises(UserError):
            job.action_requeue()

    def test_cancel(self):
        job = self.partner.with_gq_delay().gq_probe()
        job.action_cancel()
        self.assertEqual(job.state, "cancelled")
        self._run()
        self.assertFalse(self.calls)
        started = self._make_started(minutes_ago=0, attempts=1)
        with self.assertRaises(UserError):
            started.action_cancel()

    def test_actions_require_write_access(self):
        job = self.partner.with_gq_delay().gq_probe()
        with self.assertRaises(AccessError):
            job.with_user(self.user).action_cancel()
        with self.assertRaises(AccessError):
            job.with_user(self.user).action_requeue()

    # ── Grupo y dependencias ──────────────────────────────────────────────────

    def _partners(self, count):
        return self.env["res.partner"].create(
            [{"name": f"GQ {i}"} for i in range(count)]
        )

    def test_chunks_share_group(self):
        jobs = self._partners(5).with_gq_delay(chunk_size=2).gq_probe()
        self.assertTrue(jobs[0].group_uuid)
        self.assertEqual(len(set(jobs.mapped("group_uuid"))), 1)
        self.assertEqual(set(jobs.mapped("state")), {"pending"})
        single = self.partner.with_gq_delay().gq_probe()
        self.assertFalse(single.group_uuid)

    def test_depends_on_forces_order(self):
        first = self.partner.with_gq_delay(priority=50).gq_probe("first")
        second = self.partner.with_gq_delay(priority=1, depends_on=first).gq_probe(
            "second"
        )
        self.assertEqual(second.state, "waiting")
        self.assertEqual(second.depends_on_ids, first)
        self.assertTrue(first.group_uuid)
        self.assertEqual(first.group_uuid, second.group_uuid)
        self._run()
        self.assertEqual((first.state, second.state), ("done", "done"))
        self.assertEqual(
            [call["args"] for call in self.calls], [("first",), ("second",)]
        )

    def test_depends_on_done_job_runs_at_once(self):
        first = self.partner.with_gq_delay().gq_probe()
        self._run()
        second = self.partner.with_gq_delay(depends_on=first).gq_probe()
        self.assertEqual(second.state, "pending")

    def test_job_after_all_chunks(self):
        jobs = self._partners(4).with_gq_delay(chunk_size=2).gq_probe("lote")
        final = self.partner.with_gq_delay(priority=1, depends_on=jobs).gq_probe("fin")
        self.assertEqual(final.state, "waiting")
        self.assertEqual(final.group_uuid, jobs[0].group_uuid)
        self._run()
        self.assertEqual(final.state, "done")
        self.assertEqual(self.calls[-1]["args"], ("fin",))
        self.assertEqual(final.group_job_count, 3)
        self.assertEqual(final.group_done_count, 3)

    def test_failed_job_stops_group(self):
        partners = self._partners(6)
        jobs = partners.with_gq_delay(chunk_size=2).gq_probe()
        final = self.partner.with_gq_delay(depends_on=jobs).gq_probe("fin")
        outsider = self.partner.with_gq_delay().gq_probe("otro")
        self.fail_ids = {partners[0].id}
        self._run()
        self.assertEqual(jobs[0].state, "failed")
        self.assertEqual(
            jobs[1:].mapped("state"),
            ["waiting", "waiting"],
            "El fallo de un job detiene los demas del grupo",
        )
        self.assertEqual(final.state, "waiting")
        self.assertIn("Grupo detenido", jobs[1].result)
        self.assertEqual(outsider.state, "done", "Jobs fuera del grupo siguen")

        # Reencolar el fallido reanuda el grupo
        self.fail_ids = set()
        jobs[0].action_requeue()
        self._run()
        self.assertEqual(set(jobs.mapped("state")), {"done"})
        self.assertEqual(final.state, "done")

    def test_explicit_group(self):
        a = self.partner.with_gq_delay(group="import-1").gq_probe("a")
        b = self.partner.with_gq_delay(group="import-1").gq_probe("b")
        self.assertEqual((a.state, b.state), ("pending", "pending"))
        self.side_effect = ValueError("boom")
        a._gq_process()
        self.assertEqual((a.state, b.state), ("failed", "waiting"))
        c = self.partner.with_gq_delay(group="import-1").gq_probe("c")
        self.assertEqual(c.state, "waiting", "Un grupo detenido no admite jobs nuevos")

    def test_cancel_failed_job_resumes_independent_jobs(self):
        a = self.partner.with_gq_delay(group="g").gq_probe("a")
        b = self.partner.with_gq_delay(group="g").gq_probe("b")
        c = self.partner.with_gq_delay(depends_on=a).gq_probe("c")
        self.side_effect = ValueError("boom")
        a._gq_process()
        self.side_effect = None
        self.assertEqual((b.state, c.state), ("waiting", "waiting"))
        a.action_cancel()
        self.assertEqual(b.state, "pending")
        self.assertEqual(c.state, "waiting", "Depende de un job cancelado")

    def test_cancel_group(self):
        partners = self._partners(4)
        jobs = partners.with_gq_delay(chunk_size=2).gq_probe()
        final = self.partner.with_gq_delay(depends_on=jobs).gq_probe()
        self.fail_ids = {partners[0].id}
        self._run()
        jobs[0].action_cancel_group()
        self.assertEqual(set((jobs | final).mapped("state")), {"cancelled"})
        self._run()
        self.assertEqual(len(self.calls), 1)

    def test_orphan_failure_stops_group(self):
        a = self.partner.with_gq_delay(group="g").gq_probe("a")
        b = self.partner.with_gq_delay(group="g").gq_probe("b")
        a.write(
            {
                "state": "started",
                "date_started": fields.Datetime.now() - timedelta(minutes=5),
                "retry_count": 6,
            }
        )
        with patch.object(type(self.Job), "_gq_time_limit", lambda self: 120):
            self.Job._gq_recover_started_jobs()
        self.assertEqual((a.state, b.state), ("failed", "waiting"))

    def test_requeue_waiting_job_is_rejected(self):
        first = self.partner.with_gq_delay().gq_probe()
        second = self.partner.with_gq_delay(depends_on=first).gq_probe()
        with self.assertRaises(UserError):
            second.action_requeue()

    # ── Limpieza ──────────────────────────────────────────────────────────────

    def test_gc_finished_jobs(self):
        old = fields.Datetime.now() - timedelta(days=40)
        recent = fields.Datetime.now() - timedelta(days=5)
        jobs = self.Job
        for state, date_done in (
            ("done", old),
            ("cancelled", old),
            ("failed", old),
            ("done", recent),
        ):
            job = self.partner.with_gq_delay().gq_probe()
            job.write({"state": state, "date_done": date_done})
            jobs |= job
        self.Job._gc_finished_jobs()
        self.assertEqual(
            jobs.exists().mapped("state"),
            ["failed", "done"],
            "Solo se borran hechos y cancelados fuera del periodo de retencion",
        )

    def test_gc_disabled(self):
        self.env["ir.config_parameter"].set_param("gq_queue.keep_done_days", "0")
        job = self.partner.with_gq_delay().gq_probe()
        job.write(
            {"state": "done", "date_done": fields.Datetime.now() - timedelta(days=400)}
        )
        self.Job._gc_finished_jobs()
        self.assertTrue(job.exists())

    def test_pending_index_exists(self):
        self.env.cr.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = 'gq_job_pending_idx'"
        )
        self.assertTrue(self.env.cr.fetchone())
