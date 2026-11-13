{
    "name": "GQ Queue",
    "version": "18.0.3.0.0",
    "summary": "Asynchronous job queue executed via Odoo cron workers",
    "author": "Custom",
    "license": "LGPL-3",
    "category": "Technical",
    "depends": ["base", "web", "mail"],
    "data": [
        "security/gq_queue_security.xml",
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "data/mail_message_subtype.xml",
        "views/gq_job_views.xml",
        "views/gq_job_function_views.xml",
        "views/ir_cron_views.xml",
        "wizards/gq_job_mass_action_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "gq_queue/static/src/gq_job_graph/*",
        ],
        "web.assets_tests": [
            "gq_queue/static/tests/tours/*",
        ],
    },
    "installable": True,
    "application": False,
}
