import json
import logging
import traceback
from datetime import timedelta
from io import StringIO

from psycopg2 import OperationalError

from odoo import _, api, fields, models, tools
from odoo.service.model import PG_CONCURRENCY_ERRORS_TO_RETRY

from ..delay import GQJobEncoder, gq_job_decoder
from ..exception import GQFailedJobError, GQNothingToDoJob, GQRetryableJobError

_logger = logging.getLogger(__name__)

# Segundos a esperar tras un error de concurrencia de PostgreSQL
PG_RETRY = 5

GQ_PENDING = "pending"
GQ_STARTED = "started"
GQ_DONE = "done"
GQ_FAILED = "failed"
GQ_CANCELLED = "cancelled"


class GQJob(models.Model):
    _name = "gq.job"
    _description = "GQ Queue Job"
    _order = "priority asc, date_created asc"

    # ── Identificacion ────────────────────────────────────────────────────────

    name = fields.Char(string="Description", readonly=True)
    uuid = fields.Char(
        string="UUID",
        readonly=True,
        index=True,
        default=lambda self: str(__import__("uuid").uuid4()),
        copy=False,
    )

    # ── Que ejecutar ──────────────────────────────────────────────────────────

    model_name = fields.Char(string="Model", readonly=True, required=True)
    method_name = fields.Char(string="Method", readonly=True, required=True)
    record_ids = fields.Text(
        string="Record IDs",
        readonly=True,
        help="JSON array con los IDs del recordset sobre el que se ejecuta el metodo.",
    )
    args = fields.Text(
        string="Arguments",
        readonly=True,
        help="JSON array con los argumentos posicionales.",
    )
    kwargs = fields.Text(
        string="Keyword Arguments",
        readonly=True,
        help="JSON object con los argumentos nombrados.",
    )

    # ── Estado ────────────────────────────────────────────────────────────────

    state = fields.Selection(
        selection=[
            (GQ_PENDING, "Pending"),
            (GQ_STARTED, "Started"),
            (GQ_DONE, "Done"),
            (GQ_FAILED, "Failed"),
            (GQ_CANCELLED, "Cancelled"),
        ],
        string="State",
        default=GQ_PENDING,
        index=True,
        readonly=True,
    )

    # ── Configuracion ─────────────────────────────────────────────────────────

    priority = fields.Integer(
        string="Priority",
        default=10,
        help="Menor numero = mayor prioridad. Se procesa antes.",
    )
    eta = fields.Datetime(
        string="Execute After",
        help="No ejecutar el job antes de esta fecha/hora.",
    )
    max_retries = fields.Integer(
        string="Max Retries",
        default=5,
        help="Maximo de reintentos en caso de error retryable. 0 = sin limite.",
    )
    retry_count = fields.Integer(
        string="Retry Count",
        default=0,
        readonly=True,
    )
    channel = fields.Char(
        string="Channel",
        default="root",
        help="Canal logico del job. Actualmente informativo.",
    )

    # ── Resultado ─────────────────────────────────────────────────────────────

    result = fields.Text(string="Result", readonly=True)
    exc_info = fields.Text(string="Exception Info", readonly=True)

    # ── Fechas ────────────────────────────────────────────────────────────────

    date_created = fields.Datetime(
        string="Created",
        default=fields.Datetime.now,
        readonly=True,
        index=True,
    )
    date_started = fields.Datetime(string="Started At", readonly=True)
    date_done = fields.Datetime(string="Finished At", readonly=True)

    # ── Maquina de estados ────────────────────────────────────────────────────

    def _gq_set_started(self):
        self.sudo().write(
            {"state": GQ_STARTED, "date_started": fields.Datetime.now()}
        )

    def _gq_set_done(self, result=None):
        vals = {"state": GQ_DONE, "date_done": fields.Datetime.now()}
        if result is not None:
            vals["result"] = str(result)
        self.sudo().write(vals)

    def _gq_set_failed(self, exc_info=None):
        vals = {"state": GQ_FAILED, "date_done": fields.Datetime.now()}
        if exc_info:
            vals["exc_info"] = exc_info
        self.sudo().write(vals)

    def _gq_set_cancelled(self):
        self.sudo().write(
            {"state": GQ_CANCELLED, "date_done": fields.Datetime.now()}
        )

    def _gq_postpone_pending(self, result=None, seconds=PG_RETRY, reset_retry=False):
        """Volver a pending con una nueva ETA (para reintentos)."""
        vals = {
            "state": GQ_PENDING,
            "eta": fields.Datetime.now() + timedelta(seconds=seconds),
        }
        if not reset_retry:
            vals["retry_count"] = self.retry_count + 1
        if result is not None:
            vals["result"] = str(result)
        self.sudo().write(vals)

    # ── Ejecucion ─────────────────────────────────────────────────────────────

    def _gq_can_retry(self):
        """True si todavia quedan reintentos disponibles."""
        return self.max_retries == 0 or self.retry_count < self.max_retries

    def _gq_perform(self):
        """Deserializa y ejecuta el metodo almacenado en el job."""
        self.ensure_one()
        model = self.env[self.model_name]
        record_ids = json.loads(self.record_ids or "[]")
        args = json.loads(self.args or "[]", object_hook=gq_job_decoder)
        kwargs = json.loads(self.kwargs or "{}", object_hook=gq_job_decoder)

        recordset = model.browse(record_ids) if record_ids else model
        method = getattr(recordset, self.method_name)
        return method(*args, **kwargs)

    def _gq_process(self, commit=False):
        """Ejecuta el job y gestiona todos los casos de error."""
        self.ensure_one()
        self._gq_set_started()
        _logger.debug(
            "GQ Job #%s started: %s.%s", self.id, self.model_name, self.method_name
        )

        try:
            try:
                with self.env.cr.savepoint():
                    self._gq_perform()
                    self._gq_set_done()

            except OperationalError as err:
                # Errores de concurrencia de PostgreSQL: reintentar automaticamente
                if err.pgcode not in PG_CONCURRENCY_ERRORS_TO_RETRY:
                    raise
                message = tools.ustr(err.pgerror, errors="replace")
                self._gq_postpone_pending(result=message, seconds=PG_RETRY, reset_retry=True)
                _logger.debug("GQ Job #%s OperationalError, postponed %ss", self.id, PG_RETRY)

        except GQNothingToDoJob as err:
            msg = str(err) or _("Job interrupted: nothing to do.")
            self._gq_set_done(result=msg)

        except GQRetryableJobError as err:
            if self._gq_can_retry():
                seconds = getattr(err, "seconds", None) or 5
                self._gq_postpone_pending(result=str(err), seconds=seconds)
                _logger.debug("GQ Job #%s postponed %ss (retryable)", self.id, seconds)
            else:
                with StringIO() as buff:
                    traceback.print_exc(file=buff)
                    exc_text = buff.getvalue()
                _logger.error("GQ Job #%s max retries exceeded:\n%s", self.id, exc_text)
                self._gq_set_failed(exc_info=exc_text)

        except (GQFailedJobError, Exception):
            with StringIO() as buff:
                traceback.print_exc(file=buff)
                exc_text = buff.getvalue()
            _logger.error("GQ Job #%s failed:\n%s", self.id, exc_text)
            self._gq_set_failed(exc_info=exc_text)

        if commit:
            self.env.flush_all()
            self.env.cr.commit()  # pylint: disable=invalid-commit

        _logger.debug("GQ Job #%s finished with state: %s", self.id, self.state)

    # ── Runner principal ──────────────────────────────────────────────────────

    @api.model
    def _gq_acquire_one_job(self):
        """Toma el siguiente job pendiente y lo bloquea para evitar procesamiento duplicado.

        Usa FOR NO KEY UPDATE SKIP LOCKED para que multiples CronWorkers
        puedan correr en paralelo sin pisar el mismo job.

        :returns: recordset gq.job (bloqueado), o vacio si no hay pendientes.
        """
        self.env.flush_all()
        self.env.cr.execute(
            """
            SELECT id
            FROM gq_job
            WHERE state = 'pending'
              AND (eta IS NULL OR eta <= (now() AT TIME ZONE 'UTC'))
            ORDER BY priority ASC, date_created ASC
            LIMIT 1 FOR NO KEY UPDATE SKIP LOCKED
            """
        )
        row = self.env.cr.fetchone()
        return self.browse(row and row[0])

    @api.model
    def _gq_job_runner(self, commit=True):
        """Punto de entrada del cron: procesa todos los jobs pendientes disponibles.

        Llamado por el ir.cron con gq_job_runner=True.
        El loop continua hasta que no queden jobs pendientes.
        """
        _logger.debug("GQ Job Runner started")
        job = self._gq_acquire_one_job()
        processed = 0
        while job:
            job._gq_process(commit=commit)
            processed += 1
            job = self._gq_acquire_one_job()
        _logger.debug("GQ Job Runner finished (%d jobs processed)", processed)

    # ── Triggers de cron ──────────────────────────────────────────────────────

    @api.model
    def _gq_cron_trigger(self, at=None):
        """Dispara todos los crons marcados como gq_job_runner.

        Si hay multiples crons (para paralelismo), se disparan todos.
        :param at: datetime opcional; si se omite, dispara inmediatamente.
        """
        crons = self.env["ir.cron"].sudo().search([("gq_job_runner", "=", True)])
        for cron in crons:
            cron._trigger(at=at)

    def _gq_ensure_cron_trigger(self):
        """Crea triggers de cron cuando hay jobs pendientes en este recordset."""
        pending = self.filtered(lambda r: r.state == GQ_PENDING)
        if not pending:
            return
        # Jobs sin ETA: disparar inmediatamente
        if any(not rec.eta for rec in pending):
            self._gq_cron_trigger()
        # Jobs con ETA futura: programar el cron para esa hora
        delayed_etas = {rec.eta for rec in pending if rec.eta}
        if delayed_etas:
            self._gq_cron_trigger(at=list(delayed_etas))

    # ── Hooks ORM ─────────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._gq_ensure_cron_trigger()
        return records

    def write(self, vals):
        res = super().write(vals)
        if "state" in vals or "eta" in vals:
            self._gq_ensure_cron_trigger()
        return res
