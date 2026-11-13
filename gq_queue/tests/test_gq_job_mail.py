from unittest.mock import patch

from odoo.tests import new_test_user, tagged

from .common import GQCommon


@tagged("post_install", "-at_install")
class TestGQJobMail(GQCommon):
    """Chatter del job y aviso por fallo a sus seguidores."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manager = new_test_user(
            cls.env,
            login="gq_mail_manager",
            groups="base.group_user,gq_queue.group_gq_queue_manager",
        )
        cls.subtype = cls.env.ref("gq_queue.mt_job_failed")

    def _failure_messages(self, job):
        return job.message_ids.filtered(lambda msg: msg.subtype_id == self.subtype)

    def test_enqueue_is_quiet(self):
        job = self.partner.with_user(self.user).with_gq_delay().gq_probe()
        self.assertFalse(job.message_ids)
        self.assertFalse(job.message_follower_ids)

    def test_failure_posts_and_subscribes(self):
        self.side_effect = ValueError("boom")
        job = self.partner.with_user(self.user).with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "failed")
        messages = self._failure_messages(job)
        self.assertEqual(len(messages), 1)
        self.assertIn("ValueError", messages.body)
        self.assertIn("boom", messages.body)
        followers = job.message_partner_ids
        self.assertIn(self.manager.partner_id, followers)
        self.assertIn(self.user.partner_id, followers, "Quien encolo se entera")

    def test_no_message_on_success_or_retry(self):
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        self.assertEqual(job.state, "done")
        self.assertFalse(job.message_ids)

    def test_dead_job_posts(self):
        job = self._make_started(minutes_ago=5, attempts=6)
        self.Job._gq_recover_started_jobs()
        self.assertEqual(job.state, "failed")
        self.assertIn("GQDeadJobError", self._failure_messages(job).body)

    def test_followers_limited_to_job_company(self):
        other = self.env["res.company"].create({"name": "GQ Mail Other"})
        self.side_effect = ValueError("boom")
        job = self.partner.with_company(other).with_gq_delay().gq_probe()
        job.user_id = False
        self._run()
        self.assertNotIn(self.manager.partner_id, job.message_partner_ids)
        job.company_id = False
        self.assertIn(self.manager, job._gq_failure_followers())

    def test_empty_message_is_not_posted(self):
        self.side_effect = ValueError("boom")
        job = self.partner.with_gq_delay().gq_probe()
        with patch.object(type(self.Job), "_gq_message_failed_job", return_value=""):
            self._run()
        self.assertEqual(job.state, "failed")
        self.assertFalse(self._failure_messages(job))
        self.assertIn(self.manager.partner_id, job.message_partner_ids)

    def test_notification_error_does_not_break_runner(self):
        self.side_effect = ValueError("boom")
        job = self.partner.with_gq_delay().gq_probe()
        with (
            patch.object(
                type(self.Job), "_gq_message_failed_job", side_effect=KeyError("x")
            ),
            self.assertLogs("odoo.addons.gq_queue.models.gq_job", "ERROR") as logs,
        ):
            self._run()
        self.assertEqual(job.state, "failed")
        self.assertTrue(
            any("error al avisar del fallo" in line for line in logs.output)
        )
        self.assertFalse(job.message_follower_ids, "El aviso se revirtio entero")
