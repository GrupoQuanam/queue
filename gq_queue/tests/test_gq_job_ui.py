from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from ..models import gq_job as gq_job_module
from .common import GQCommon


@tagged("post_install", "-at_install")
class TestGQJobUI(GQCommon):
    """Permisos, accion relacionada, acciones masivas y grafo de dependencias."""

    # ── Permisos ──────────────────────────────────────────────────────────────

    def test_admins_are_queue_managers(self):
        admin = new_test_user(self.env, login="gq_admin", groups="base.group_system")
        self.assertTrue(admin.has_group("gq_queue.group_gq_queue_manager"))

    def test_manager_without_admin_rights(self):
        manager = new_test_user(
            self.env,
            login="gq_manager",
            groups="base.group_user,gq_queue.group_gq_queue_manager",
        )
        job = self.partner.with_gq_delay().gq_probe()
        self.assertEqual(job.with_user(manager).read(["state"])[0]["state"], "pending")
        job.with_user(manager).action_cancel()
        self.assertEqual(job.state, "cancelled")
        with self.assertRaises(AccessError):
            job.with_user(self.user).read(["state"])

    def test_multi_company_rule(self):
        other = self.env["res.company"].create({"name": "GQ Other"})
        manager = new_test_user(
            self.env,
            login="gq_manager_mc",
            groups="base.group_user,gq_queue.group_gq_queue_manager",
        )
        mine = self.partner.with_gq_delay().gq_probe("mine")
        theirs = self.partner.with_company(other).with_gq_delay().gq_probe("theirs")
        shared = self.partner.with_gq_delay().gq_probe("shared")
        shared.company_id = False
        visible = self.Job.with_user(manager).search(
            [("id", "in", (mine | theirs | shared).ids)]
        )
        self.assertEqual(visible, mine | shared)

    # ── Marcar como hecho ─────────────────────────────────────────────────────

    def test_set_done_releases_dependents(self):
        first = self.partner.with_gq_delay().gq_probe("first")
        second = self.partner.with_gq_delay(depends_on=first).gq_probe("second")
        self.assertEqual(second.state, "waiting")
        first.action_set_done()
        self.assertEqual(first.state, "done")
        self.assertIn("Manually set to done", first.result)
        self.assertTrue(first.date_done)
        self.assertEqual(second.state, "pending")
        self._run()
        self.assertEqual([call["args"] for call in self.calls], [("second",)])

    def test_set_done_resumes_failed_group(self):
        self.side_effect = ValueError("boom")
        failing = self.partner.with_gq_delay(group="g").gq_probe("failing")
        other = self.partner.with_gq_delay(group="g", priority=20).gq_probe("other")
        self._run()
        self.assertEqual(failing.state, "failed")
        self.assertEqual(other.state, "waiting")
        self.side_effect = None
        (failing | other).action_set_done()
        self.assertEqual(failing.state, "done")
        self.assertEqual(other.state, "done", "Marcar hecho no ejecuta el job")
        self.assertEqual(len(self.calls), 1)

    def test_set_done_keeps_done_jobs(self):
        job = self.partner.with_gq_delay().gq_probe()
        self._run()
        date_done = job.date_done
        job.action_set_done()
        self.assertNotIn("Manually", job.result or "")
        self.assertEqual(job.date_done, date_done)

    def test_set_done_running_job_is_rejected(self):
        job = self._make_started(minutes_ago=1, attempts=1)
        with self.assertRaises(UserError):
            job.action_set_done()

    # ── Accion relacionada ────────────────────────────────────────────────────

    def test_related_action_single_record(self):
        job = self.partner.with_gq_delay().gq_probe()
        action = job.action_open_related()
        self.assertEqual(action["res_model"], "res.partner")
        self.assertEqual(action["res_id"], self.partner.id)

    def test_related_action_many_records(self):
        partners = self._partners(2)
        job = partners.with_gq_delay().gq_probe()
        action = job.action_open_related()
        self.assertEqual(action["domain"], [("id", "in", partners.ids)])
        self.assertNotIn("res_id", action)

    def test_related_action_without_records(self):
        partner = self._partners(1)
        job = partner.with_gq_delay().gq_probe()
        partner.unlink()
        with self.assertRaises(UserError):
            job.action_open_related()
        job.model_name = "gq.no.such.model"
        with self.assertRaises(UserError):
            job.action_open_related()

    def test_related_action_disabled(self):
        self._configure(related_action_enable=False)
        job = self.partner.with_gq_delay().gq_probe()
        with self.assertRaises(UserError):
            job.action_open_related()

    def test_related_action_custom_function(self):
        self._configure(related_action_method="action_view_group")
        job = self.partner.with_gq_delay(group="g").gq_probe()
        action = job.action_open_related()
        self.assertEqual(action["res_model"], "gq.job")
        self.assertEqual(action["domain"], [("group_uuid", "=", job.group_uuid)])

    def test_related_action_function_must_exist(self):
        with self.assertRaises(ValidationError):
            self._configure(related_action_method="gq_no_such_function")

    # ── Acciones masivas ──────────────────────────────────────────────────────

    def _wizard(self, operation, jobs):
        return (
            self.env["gq.job.mass.action"]
            .with_context(
                active_model="gq.job",
                active_ids=jobs.ids,
                default_operation=operation,
            )
            .create({})
        )

    def test_mass_actions(self):
        jobs = (
            self.partner.with_gq_delay().gq_probe()
            | self.partner.with_gq_delay(identity_key="other").gq_probe()
        )
        wizard = self._wizard("cancel", jobs)
        self.assertEqual(wizard.job_count, 2)
        self.assertEqual(wizard.action_apply(), {"type": "ir.actions.act_window_close"})
        self.assertEqual(set(jobs.mapped("state")), {"cancelled"})
        self._wizard("requeue", jobs).action_apply()
        self.assertEqual(set(jobs.mapped("state")), {"pending"})
        self._wizard("set_done", jobs).action_apply()
        self.assertEqual(set(jobs.mapped("state")), {"done"})

    def test_mass_action_without_selection(self):
        wizard = (
            self.env["gq.job.mass.action"]
            .with_context(default_operation="cancel")
            .create({})
        )
        self.assertFalse(wizard.job_ids)
        self.assertEqual(wizard.job_count, 0)

    # ── Grafo de dependencias ─────────────────────────────────────────────────

    def test_no_graph_for_single_job(self):
        job = self.partner.with_gq_delay().gq_probe()
        self.assertFalse(job.has_graph)
        self.assertFalse(job.dependency_graph)

    def test_graph_of_dependencies_and_group(self):
        first = self.partner.with_gq_delay().gq_probe("first")
        second = self.partner.with_gq_delay(depends_on=first).gq_probe("second")
        third = self.partner.with_gq_delay(depends_on=second).gq_probe("third")
        # Mismo grupo que nada mas: conectado solo por el grupo
        grouped = self.partner.with_gq_delay(group="g").gq_probe("grouped")
        sibling = self.partner.with_gq_delay(group="g").gq_probe("sibling")
        # Solo dependencias, sin grupo
        (first | second | third).group_uuid = False
        self.assertTrue(second.has_graph)
        graph = second.dependency_graph
        self.assertEqual(graph["current"], second.id)
        self.assertFalse(graph["truncated"])
        self.assertEqual(
            {node["id"] for node in graph["nodes"]}, {first.id, second.id, third.id}
        )
        self.assertEqual(
            sorted(map(tuple, graph["edges"])),
            sorted([(first.id, second.id), (second.id, third.id)]),
        )
        node = next(node for node in graph["nodes"] if node["id"] == second.id)
        self.assertEqual(node["state"], "waiting")
        group_graph = grouped.dependency_graph
        self.assertEqual(
            {node["id"] for node in group_graph["nodes"]}, {grouped.id, sibling.id}
        )
        self.assertEqual(group_graph["edges"], [])

    def test_graph_is_truncated(self):
        jobs = self.partner.with_gq_delay(group="big").gq_probe("a")
        for name in "bcde":
            jobs |= self.partner.with_gq_delay(group="big").gq_probe(name)
        with patch.object(gq_job_module, "MAX_GRAPH_NODES", 3):
            jobs.invalidate_recordset(["dependency_graph"])
            graph = jobs[0].dependency_graph
        self.assertTrue(graph["truncated"])
        self.assertEqual(len(graph["nodes"]), 3)

    def test_graph_of_new_record(self):
        self.assertFalse(self.Job.new({}).dependency_graph)
