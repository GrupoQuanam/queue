import os
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tools import mute_logger

from ..delay import GQDelayable, GQGraphNode, gq_chain, gq_group
from .common import GQCommon


@tagged("post_install", "-at_install")
class TestGQDelay(GQCommon):
    """Interfaz de encolado: grafos, ejecucion en linea, encolado automatico
    y funciones que hacen commit."""

    def _labels(self):
        return [call["args"][0] for call in self.calls]

    def _deferred(self, label, **options):
        return self.partner.gq_delayable(**options).gq_probe(label)

    # ── Opciones ──────────────────────────────────────────────────────────────

    def test_unknown_option(self):
        with self.assertRaises(TypeError):
            GQDelayable(self.partner, prioridad=1)

    # ── Ejecucion en linea ────────────────────────────────────────────────────

    def test_no_delay_from_context(self):
        partner = self.partner.with_context(gq_queue__no_delay=True)
        result = partner.with_gq_delay().gq_probe("x")
        self.assertEqual(result, self.partner.ids, "Retorna el resultado de la funcion")
        self.assertEqual(self._labels(), ["x"])
        self.assertFalse(self.Job.search([("method_name", "=", "gq_probe")]))

    def test_no_delay_from_environment(self):
        with patch.dict(os.environ, {"GQ_QUEUE__NO_DELAY": "1"}):
            self.partner.with_gq_delay().gq_probe("x")
        self.assertEqual(self._labels(), ["x"])
        with patch.dict(os.environ, {"GQ_QUEUE__NO_DELAY": "0"}):
            job = self.partner.with_gq_delay().gq_probe("y")
        self.assertEqual(job.state, "pending")
        self.assertEqual(self._labels(), ["x"])

    def test_no_delay_with_chunks(self):
        partners = self._partners(3).with_context(gq_queue__no_delay=True)
        result = partners.with_gq_delay(chunk_size=2).gq_probe("x")
        self.assertEqual(result, [partners[:2].ids, partners[2:].ids])

    # ── Llamadas diferidas ────────────────────────────────────────────────────

    def test_deferred_is_enqueued_on_delay(self):
        deferred = self._deferred("a")
        self.assertFalse(self.Job.search([("method_name", "=", "gq_probe")]))
        job = deferred.delay()
        self.assertEqual(job.state, "pending")
        self.assertFalse(job.group_uuid, "Un job solo no forma grupo")
        self._run()
        self.assertEqual(self._labels(), ["a"])

    def test_deferred_set_options(self):
        deferred = self._deferred("a", priority=50).set(
            priority=3, description="Paso A"
        )
        job = deferred.delay()
        self.assertEqual(job.priority, 3)
        self.assertEqual(job.name, "Paso A")
        with self.assertRaises(TypeError):
            deferred.set(prioridad=1)
        with self.assertRaises(ValueError):
            self._deferred("b", identity_key="k").set(chunk_size=2)

    def test_deferred_misuse(self):
        with self.assertRaises(ValueError):
            self.partner.gq_delayable().delay()
        with self.assertRaises(ValueError):
            self._deferred("a").gq_probe("b")
        with self.assertRaises(AttributeError):
            self.partner.gq_delayable().gq_no_such_function()
        with self.assertRaises(ValueError):
            gq_group()
        with self.assertRaises(ValueError):
            gq_chain()
        node = GQGraphNode()
        with self.assertRaises(NotImplementedError):
            node._heads()
        with self.assertRaises(NotImplementedError):
            node._tails()

    # ── Cadenas y grupos ──────────────────────────────────────────────────────

    def test_chain_runs_in_order(self):
        a = self._deferred("a", priority=90)
        b = self._deferred("b", priority=50)
        c = self._deferred("c", priority=1)
        jobs = gq_chain(a, b, c).delay()
        self.assertEqual(jobs.mapped("state"), ["pending", "waiting", "waiting"])
        self.assertEqual(len(set(jobs.mapped("group_uuid"))), 1)
        self.assertTrue(jobs[0].group_uuid)
        self.assertEqual(jobs[2].depends_on_ids, jobs[1])
        self._run()
        self.assertEqual(self._labels(), ["a", "b", "c"])

    def test_group_then_on_done(self):
        a = self._deferred("a")
        b = self._deferred("b")
        end = self._deferred("end", priority=1)
        jobs = gq_group(a, b).on_done(end).delay()
        self.assertEqual(jobs.mapped("state"), ["pending", "pending", "waiting"])
        self.assertEqual(jobs[2].depends_on_ids, jobs[:2])
        self._run()
        self.assertEqual(self._labels(), ["a", "b", "end"])

    def test_chain_with_group_inside(self):
        start = self._deferred("start")
        b = self._deferred("b")
        c = self._deferred("c")
        end = self._deferred("end", priority=1)
        jobs = gq_chain(start, gq_group(b, c), end).delay()
        by_label = {job.args: job for job in jobs}
        self.assertEqual(by_label['["end"]'].depends_on_ids, jobs[1:3])
        self.assertEqual(by_label['["b"]'].depends_on_ids, jobs[0])
        self._run()
        self.assertEqual(self._labels(), ["start", "b", "c", "end"])

    def test_on_done_with_several_nodes_and_a_chain(self):
        a = self._deferred("a")
        b = self._deferred("b")
        c = self._deferred("c")
        d = self._deferred("d", priority=1)
        a.on_done(b, gq_chain(c, d))
        jobs = d.delay()
        self.assertEqual(len(jobs), 4, "delay() encola todo el grafo conectado")
        self._run()
        self.assertEqual(self._labels(), ["a", "b", "c", "d"])

    def test_chain_then_on_done(self):
        a = self._deferred("a")
        b = self._deferred("b")
        end = self._deferred("end", priority=1)
        jobs = gq_chain(a, b).on_done(end).delay()
        self.assertEqual(
            jobs[2].depends_on_ids, jobs[1], "Espera al final de la cadena"
        )
        self._run()
        self.assertEqual(self._labels(), ["a", "b", "end"])

    def test_failure_stops_the_chain(self):
        a = self._deferred("a")
        b = self._deferred("b")
        jobs = gq_chain(a, b).delay()
        self.side_effect = ValueError("boom")
        self._run()
        self.assertEqual(jobs.mapped("state"), ["failed", "waiting"])
        self.assertEqual(self._labels(), ["a"])

    def test_circular_dependency(self):
        a = self._deferred("a")
        b = self._deferred("b")
        a.on_done(b)
        b.on_done(a)
        with self.assertRaises(ValueError):
            a.delay()

    def test_chain_with_chunks(self):
        partners = self._partners(4)
        lotes = partners.gq_delayable(chunk_size=2).gq_probe("lote")
        end = self._deferred("end", priority=1)
        jobs = gq_chain(lotes, end).delay()
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[2].depends_on_ids, jobs[:2])
        self._run()
        self.assertEqual(self._labels(), ["lote", "lote", "end"])

    def test_graph_options(self):
        existing = self.partner.with_gq_delay(identity_key="k").gq_probe("previo")
        a = self._deferred("a", identity_key="k")
        b = self._deferred("b", group="mi-grupo")
        jobs = gq_chain(a, b).delay()
        self.assertEqual(jobs[0], existing, "Reutiliza el job con la misma clave")
        self.assertEqual(jobs[1].group_uuid, "mi-grupo")
        self.assertEqual(jobs[1].depends_on_ids, existing)

    def test_graph_no_delay(self):
        partner = self.partner.with_context(gq_queue__no_delay=True)
        a = partner.gq_delayable().gq_probe("a")
        b = partner.gq_delayable().gq_probe("b")
        self.assertIsNone(gq_chain(a, b).delay())
        self.assertEqual(self._labels(), ["a", "b"])
        self.assertFalse(self.Job.search([("method_name", "=", "gq_probe")]))

    # ── Encolado automatico ───────────────────────────────────────────────────

    def _patch_auto_delay(self, context_key=None, with_options=False):
        test = self
        partner_cls = type(self.env["res.partner"])

        def gq_auto(records, label):
            test.calls.append({"args": (label,), "ids": records.ids})
            return "resultado"

        def gq_auto_gq_job_options(records, label):
            return {"priority": 77, "description": f"Auto {label}"}

        functions = {"gq_auto": gq_auto}
        if with_options:
            functions["gq_auto_gq_job_options"] = gq_auto_gq_job_options
        for name, function in functions.items():
            patcher = patch.object(partner_cls, name, function, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Odoo registra cada cambio directo a una clase de modelo hecho en tests
        with mute_logger("odoo.tests.common"):
            partner_cls._gq_patch_method(
                "gq_auto",
                self.env["res.partner"]._patch_gq_job_auto_delay(
                    "gq_auto", context_key=context_key
                ),
            )

    def test_auto_delay_with_context_key(self):
        self._patch_auto_delay(context_key="gq_auto_delay")
        self.assertEqual(self.partner.gq_auto("directo"), "resultado")
        self.assertEqual(self._labels(), ["directo"])

        job = self.partner.with_context(gq_auto_delay=True).gq_auto("encolado")
        self.assertEqual(job._name, "gq.job")
        self.assertEqual(job.method_name, "gq_auto")
        self.assertEqual(job.priority, 10)
        self.assertEqual(self._labels(), ["directo"])
        self._run()
        self.assertEqual(job.state, "done", job.exc_info)
        self.assertEqual(self._labels(), ["directo", "encolado"])

    def test_auto_delay_always_with_options(self):
        self._patch_auto_delay(with_options=True)
        job = self.partner.gq_auto("x")
        self.assertEqual(job.priority, 77)
        self.assertEqual(job.name, "Auto x")
        self.assertFalse(self.calls)
        direct = self.partner.with_context(gq_queue__no_delay=True).gq_auto("y")
        self.assertEqual(direct, "resultado")
        self.assertEqual(self._labels(), ["y"])
