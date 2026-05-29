{
    "name": "GQ Queue",
    "version": "18.0.1.0.0",
    "summary": "Asynchronous job queue executed via Odoo cron workers",
    "author": "Custom",
    "license": "LGPL-3",
    "category": "Technical",
    "depends": ["base"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/gq_job_views.xml",
        "views/ir_cron_views.xml",
    ],
    "installable": True,
    "application": False,
}
