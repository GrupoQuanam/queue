from odoo import api, fields, models


class GQJobMassAction(models.TransientModel):
    """Aplica una accion a los jobs seleccionados en la lista."""

    _name = "gq.job.mass.action"
    _description = "GQ Queue Job Mass Action"

    operation = fields.Selection(
        selection=[
            ("requeue", "Requeue"),
            ("cancel", "Cancel"),
            ("set_done", "Set to Done"),
        ],
        required=True,
        readonly=True,
    )
    job_ids = fields.Many2many("gq.job", string="Jobs", readonly=True)
    job_count = fields.Integer(compute="_compute_job_count")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        context = self.env.context
        if context.get("active_model") == "gq.job" and context.get("active_ids"):
            res["job_ids"] = [(6, 0, context["active_ids"])]
        return res

    @api.depends("job_ids")
    def _compute_job_count(self):
        for wizard in self:
            wizard.job_count = len(wizard.job_ids)

    def action_apply(self):
        self.ensure_one()
        getattr(self.job_ids, f"action_{self.operation}")()
        return {"type": "ir.actions.act_window_close"}
