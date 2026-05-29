from odoo import fields, models


class IrCron(models.Model):
    _inherit = "ir.cron"

    gq_job_runner = fields.Boolean(
        string="GQ Job Runner",
        help="Si esta marcado, este cron se usa como runner del modulo GQ Queue.",
    )
