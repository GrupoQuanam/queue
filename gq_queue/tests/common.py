from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, new_test_user


class GQCommon(TransactionCase):
    """Base de los tests de gq_queue: usuario, partner y funciones de prueba."""

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
        self.fail_calls = []
        self.on_fail_error = None
        test = self

        def gq_on_fail(records, **kwargs):
            test.fail_calls.append(dict(kwargs, ids=records.ids))
            if test.on_fail_error:
                raise test.on_fail_error

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
        functions = {
            "gq_probe": gq_probe,
            "_gq_probe": gq_probe,
            "gq_on_fail": gq_on_fail,
        }
        for name, function in functions.items():
            patcher = patch.object(partner_cls, name, function, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _configure(self, method="gq_probe", **vals):
        """Crea la configuracion gq.job.function de una funcion de res.partner."""
        return self.env["gq.job.function"].create(
            dict(
                vals,
                model_id=self.env["ir.model"]._get_id("res.partner"),
                method=method,
            )
        )

    def _run(self):
        self.Job._gq_job_runner(commit=False)

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

    def _partners(self, count):
        return self.env["res.partner"].create(
            [{"name": f"GQ {i}"} for i in range(count)]
        )
