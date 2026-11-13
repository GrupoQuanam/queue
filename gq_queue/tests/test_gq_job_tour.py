from odoo.tests import HttpCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestGQJobTour(HttpCase):
    def test_dependency_graph_widget(self):
        """El widget dibuja el grafo y abre el job sobre el que se hace clic."""
        new_test_user(
            self.env,
            login="gq_tour_manager",
            groups="base.group_user,gq_queue.group_gq_queue_manager",
        )
        partner = self.env["res.partner"].create({"name": "GQ tour"})
        first = partner.with_gq_delay(description="GQ first").write({})
        second = partner.with_gq_delay(description="GQ second", depends_on=first).write(
            {}
        )
        partner.with_gq_delay(description="GQ third", depends_on=second).write({})
        self.start_tour(
            f"/odoo/action-gq_queue.gq_job_action/{second.id}",
            "gq_job_graph_tour",
            login="gq_tour_manager",
        )
