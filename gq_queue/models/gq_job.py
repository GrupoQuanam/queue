import json
import logging
import traceback
from datetime import timedelta
from io import StringIO

from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.service.model import PG_CONCURRENCY_ERRORS_TO_RETRY
from odoo.tools import config

from ..delay import GQJobEncoder, gq_job_decoder
from ..exception import GQFailedJobError, GQNothingToDoJob, GQRetryableJobError

_logger = logging.getLogger(__name__)

# Segundos a esperar tras un error de concurrencia de PostgreSQL
PG_RETRY = 5

# Presupuesto del runner (segundos) cuando el servidor no impone limite de tiempo
DEFAULT_RUNNER_SECONDS = 600
# Intentos permitidos a un job con max_retries = 0 cuyo worker muere al ejecutarlo
ORPHAN_MAX_ATTEMPTS = 5
# Dias que se conservan los jobs terminados (parametro gq_queue.keep_done_days)
DEFAULT_KEEP_DONE_DAYS = 30

GQ_WAITING = "waiting"
GQ_PENDING = "pending"
GQ_STARTED = "started"
GQ_DONE = "done"
GQ_FAILED = "failed"
GQ_CANCELLED = "cancelled"


class GQJob(models.Model):
    _name = "gq.job"
    _description = "GQ Queue Job"
    _order = "priority asc, date_created asc, id asc"

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

    # ── Con que entorno ejecutar ──────────────────────────────────────────────

    user_id = fields.Many2one(
        "res.users",
        string="User",
        readonly=True,
        index=True,
        ondelete="set null",
        default=lambda self: self.env.uid,
        help="Usuario que encolo el job. El job se ejecuta con este usuario.",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        readonly=True,
        ondelete="set null",
        default=lambda self: self.env.company,
        help="Compania activa al encolar el job.",
    )
    run_sudo = fields.Boolean(
        string="Superuser Mode",
        readonly=True,
        help="El job se encolo desde un recordset con sudo() y se ejecuta igual.",
    )
    context = fields.Text(
        string="Context",
        readonly=True,
        help="JSON object con las claves de contexto conservadas al encolar.",
    )

    # ── Estado ────────────────────────────────────────────────────────────────

    state = fields.Selection(
        selection=[
            (GQ_WAITING, "Waiting"),
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
        string="Attempts",
        default=0,
        readonly=True,
        help="Numero de veces que el runner inicio la ejecucion del job.",
    )
    channel = fields.Char(
        string="Channel",
        default="root",
        help="Canal logico del job. Actualmente informativo.",
    )
    identity_key = fields.Char(
        string="Identity Key",
        readonly=True,
        index=True,
        copy=False,
        help="Mientras haya un job pendiente con esta clave no se encola otro igual.",
    )

    # ── Grupo y dependencias ──────────────────────────────────────────────────

    group_uuid = fields.Char(
        string="Group",
        readonly=True,
        index=True,
        copy=False,
        help="Jobs que forman un mismo proceso. Si uno falla, el grupo se detiene: "
        "sus jobs pendientes pasan a 'waiting' hasta reencolar el fallido.",
    )
    depends_on_ids = fields.Many2many(
        "gq.job",
        relation="gq_job_dependency_rel",
        column1="job_id",
        column2="depends_on_id",
        string="Depends On",
        readonly=True,
        copy=False,
        help="El job no se ejecuta hasta que todos estos jobs esten en 'done'.",
    )
    dependent_ids = fields.Many2many(
        "gq.job",
        relation="gq_job_dependency_rel",
        column1="depends_on_id",
        column2="job_id",
        string="Dependent Jobs",
        readonly=True,
        copy=False,
    )
    group_job_count = fields.Integer(
        string="Jobs in Group", compute="_compute_group_progress"
    )
    group_done_count = fields.Integer(
        string="Done in Group", compute="_compute_group_progress"
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

    def init(self):
        # La cola se consulta por prioridad y antiguedad solo entre pendientes.
        # El id desempata los jobs creados en el mismo segundo.
        self.env.cr.execute(
            """
            DROP INDEX IF EXISTS gq_job_pending_idx;
            CREATE INDEX IF NOT EXISTS gq_job_queue_idx
            ON gq_job (priority, date_created, id)
            WHERE state = 'pending'
            """
        )

    @api.depends("group_uuid", "state")
    def _compute_group_progress(self):
        groups = [uuid for uuid in set(self.mapped("group_uuid")) if uuid]
        totals = {}
        if groups:
            rows = self.sudo()._read_group(
                [("group_uuid", "in", groups)], ["group_uuid", "state"], ["__count"]
            )
            for group_uuid, state, count in rows:
                total = totals.setdefault(group_uuid, [0, 0])
                total[0] += count
                if state == GQ_DONE:
                    total[1] += count
        for job in self:
            job.group_job_count, job.group_done_count = totals.get(
                job.group_uuid, (0, 0)
            )

    # ── Maquina de estados ────────────────────────────────────────────────────

    def _gq_set_started(self):
        self.ensure_one()
        self.sudo().write(
            {
                "state": GQ_STARTED,
                "date_started": fields.Datetime.now(),
                "retry_count": self.retry_count + 1,
            }
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
        self._gq_stop_group()

    def _gq_set_cancelled(self):
        self.sudo().write(
            {"state": GQ_CANCELLED, "date_done": fields.Datetime.now()}
        )

    def _gq_postpone_pending(self, result=None, seconds=PG_RETRY, reset_retry=False):
        """Volver a pending con una nueva ETA (para reintentos).

        :param reset_retry: si es True, el intento en curso no se cuenta.
        """
        vals = {
            "state": GQ_PENDING,
            "eta": fields.Datetime.now() + timedelta(seconds=seconds),
        }
        if reset_retry:
            vals["retry_count"] = max(self.retry_count - 1, 0)
        if result is not None:
            vals["result"] = str(result)
        self.sudo().write(vals)

    # ── Acciones de usuario ───────────────────────────────────────────────────

    def action_requeue(self):
        """Vuelve a encolar jobs terminados (fallidos, cancelados o hechos).

        El job queda en 'waiting' y pasa a 'pending' si sus dependencias estan
        hechas y su grupo no tiene otro job fallido.
        """
        self.check_access("write")
        if self.filtered(
            lambda job: job.state in (GQ_WAITING, GQ_PENDING, GQ_STARTED)
        ):
            raise UserError(
                _("Only failed, cancelled or done jobs can be requeued.")
            )
        self.sudo().write(
            {
                "state": GQ_WAITING,
                "retry_count": 0,
                "eta": False,
                "result": False,
                "exc_info": False,
                "date_started": False,
                "date_done": False,
            }
        )
        self._gq_release_waiting()
        return True

    def action_cancel(self):
        """Cancela jobs que no estan en ejecucion.

        Cancelar el job fallido de un grupo reanuda el resto del grupo, salvo
        los jobs que dependen del cancelado. Para abortar todo el grupo usar
        ``action_cancel_group``.
        """
        self.check_access("write")
        if self.filtered(lambda job: job.state == GQ_STARTED):
            raise UserError(_("A running job cannot be cancelled."))
        self.filtered(
            lambda job: job.state in (GQ_WAITING, GQ_PENDING, GQ_FAILED)
        )._gq_set_cancelled()
        self._gq_release_waiting()
        return True

    def action_cancel_group(self):
        """Cancela todos los jobs no ejecutados de los grupos de estos jobs."""
        self.check_access("write")
        groups = [uuid for uuid in set(self.mapped("group_uuid")) if uuid]
        jobs = self | self.sudo().search([("group_uuid", "in", groups)])
        jobs.filtered(
            lambda job: job.state in (GQ_WAITING, GQ_PENDING, GQ_FAILED)
        )._gq_set_cancelled()
        return True

    def action_view_group(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Group Jobs"),
            "res_model": "gq.job",
            "view_mode": "list,form",
            "domain": [("group_uuid", "=", self.group_uuid)],
        }

    # ── Grupo y dependencias ──────────────────────────────────────────────────

    def _gq_stop_group(self):
        """Detiene los grupos de estos jobs fallidos.

        Los jobs pendientes del grupo pasan a 'waiting'; los que ya estan en
        ejecucion terminan. SKIP LOCKED deja fuera a los que otro runner esta
        tomando en este momento.
        """
        groups = [uuid for uuid in set(self.mapped("group_uuid")) if uuid]
        if not groups:
            return
        self.env.flush_all()
        self.env.cr.execute(
            """
            UPDATE gq_job
            SET state = 'waiting', result = %s
            WHERE id IN (
                SELECT id FROM gq_job
                WHERE group_uuid = ANY(%s) AND state = 'pending'
                FOR NO KEY UPDATE SKIP LOCKED
            )
            RETURNING id
            """,
            ("Grupo detenido: fallo el job %s" % self.ids, groups),
        )
        rows = self.env.cr.fetchall()
        if rows:
            self.invalidate_model(["state", "result"])
            _logger.warning(
                "GQ Queue: grupo(s) %s detenido(s) por fallo de %s; %d job(s) en espera",
                groups,
                self.ids,
                len(rows),
            )

    @api.model
    def _gq_release_waiting(self):
        """Pasa a 'pending' los jobs en 'waiting' que ya pueden ejecutarse.

        Un job puede ejecutarse cuando todas sus dependencias estan en 'done'
        y su grupo no tiene ningun job en 'failed'.

        :returns: recordset de jobs liberados.
        """
        self.env.flush_all()
        self.env.cr.execute(
            """
            UPDATE gq_job
            SET state = 'pending'
            WHERE id IN (
                SELECT j.id FROM gq_job j
                WHERE j.state = 'waiting'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM gq_job_dependency_rel r
                      JOIN gq_job d ON d.id = r.depends_on_id
                      WHERE r.job_id = j.id AND d.state != 'done'
                  )
                  AND (
                      j.group_uuid IS NULL
                      OR NOT EXISTS (
                          SELECT 1 FROM gq_job f
                          WHERE f.group_uuid = j.group_uuid AND f.state = 'failed'
                      )
                  )
                FOR NO KEY UPDATE OF j SKIP LOCKED
            )
            RETURNING id
            """
        )
        released = self.browse([row[0] for row in self.env.cr.fetchall()])
        if released:
            self.invalidate_model(["state"])
            released._gq_ensure_cron_trigger()
        return released

    # ── Ejecucion ─────────────────────────────────────────────────────────────

    def _gq_can_retry(self):
        """True si todavia quedan reintentos disponibles.

        ``retry_count`` cuenta intentos iniciados (incluye el que esta en curso),
        por eso un job admite ``max_retries`` reintentos despues del primero.
        """
        return self.max_retries == 0 or self.retry_count <= self.max_retries

    def _gq_job_env(self):
        """Entorno con el usuario, modo superusuario y contexto del encolado."""
        self.ensure_one()
        context = json.loads(self.context or "{}")
        user = self.user_id.id or self.env.uid
        return self.env(user=user, su=self.run_sudo, context=context)

    def _gq_perform(self):
        """Deserializa y ejecuta el metodo almacenado en el job."""
        self.ensure_one()
        model = self._gq_job_env()[self.model_name]
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
        if commit:
            # Persistir 'started' y el intento antes de ejecutar: si el worker
            # muere, el job queda visible como huerfano con su intento contado.
            self.env.cr.commit()  # pylint: disable=invalid-commit
            if not self._gq_lock_started():
                _logger.info("GQ Job #%s tomado por otro runner, se omite", self.id)
                return
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
                message = str(err.pgerror or err)
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

    def _gq_lock_started(self):
        """Vuelve a bloquear la fila del job tras confirmar el estado 'started'.

        El bloqueo dura hasta el commit final del job y es lo que distingue a un
        job en ejecucion de uno huerfano.

        :returns: False si otro runner tomo el job entre el commit y el bloqueo.
        """
        self.ensure_one()
        self.env.cr.execute(
            """
            SELECT id
            FROM gq_job
            WHERE id = %s AND state = 'started'
            FOR NO KEY UPDATE SKIP LOCKED
            """,
            (self.id,),
        )
        return bool(self.env.cr.fetchone())

    @api.model
    def _gq_time_limit(self):
        """Segundos tras los que el servidor mata una ejecucion de cron.

        :returns: 0 si no hay limite.
        """
        limit = config["limit_time_real_cron"]
        if limit is None or limit < 0:
            limit = config["limit_time_real"]
        return max(limit or 0, 0)

    @api.model
    def _gq_recover_started_jobs(self):
        """Resetea jobs huerfanos en estado 'started' tras la muerte de su worker.

        Un job es huerfano si esta en 'started', su fila no esta bloqueada por
        ningun runner y ademas inicio hace mas tiempo que el limite del servidor.
        La condicion de tiempo evita reencolar un job vivo cuya funcion hizo
        commit (y por tanto solto el bloqueo de su fila).

        El intento ya se conto al iniciar el job: vuelve a 'pending' mientras le
        queden intentos y si no pasa a 'failed'.
        """
        grace = self._gq_time_limit()
        self.env.flush_all()
        self.env.cr.execute(
            """
            UPDATE gq_job
            SET
                state = CASE
                    WHEN retry_count > COALESCE(NULLIF(max_retries, 0), %(cap)s)
                    THEN 'failed'
                    ELSE 'pending'
                END,
                exc_info = CASE
                    WHEN retry_count > COALESCE(NULLIF(max_retries, 0), %(cap)s)
                    THEN %(message)s
                    ELSE exc_info
                END,
                date_done = CASE
                    WHEN retry_count > COALESCE(NULLIF(max_retries, 0), %(cap)s)
                    THEN (now() AT TIME ZONE 'UTC')
                    ELSE date_done
                END
            WHERE id IN (
                  SELECT id FROM gq_job
                  WHERE state = 'started'
                    AND (
                        date_started IS NULL
                        OR date_started <= (now() AT TIME ZONE 'UTC')
                            - make_interval(secs => %(grace)s)
                    )
                  FOR NO KEY UPDATE SKIP LOCKED
              )
            RETURNING id, state
            """,
            {
                "cap": ORPHAN_MAX_ATTEMPTS,
                "grace": grace,
                "message": "Job encontrado muerto: su worker termino durante la "
                "ejecucion y se agotaron los intentos",
            },
        )
        rows = self.env.cr.fetchall()
        if rows:
            ids = [r[0] for r in rows]
            self.invalidate_model(["state", "exc_info", "date_done"])
            self.browse(
                [r[0] for r in rows if r[1] == GQ_FAILED]
            )._gq_stop_group()
            _logger.warning(
                "GQ Queue: %d job(s) huerfano(s) recuperado(s): %s", len(ids), ids
            )
        if grace:
            self._gq_schedule_orphan_check(grace)

    @api.model
    def _gq_schedule_orphan_check(self, grace):
        """Programa el runner para cuando el 'started' mas antiguo pueda ser huerfano.

        Sin esto, un job cuyo worker murio esperaria al intervalo del cron.
        """
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT min(date_started) FROM gq_job WHERE state = 'started'"
        )
        oldest = self.env.cr.fetchone()[0]
        if not oldest:
            return
        check_at = oldest + timedelta(seconds=grace + 1)
        if check_at > fields.Datetime.now():
            self._gq_cron_trigger(at=check_at)

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
            ORDER BY priority ASC, date_created ASC, id ASC
            LIMIT 1 FOR NO KEY UPDATE SKIP LOCKED
            """
        )
        row = self.env.cr.fetchone()
        return self.browse(row and row[0])

    @api.model
    def _gq_job_runner(self, commit=True, max_seconds=None):
        """Punto de entrada del cron: procesa todos los jobs pendientes disponibles.

        Llamado por el ir.cron con gq_job_runner=True.
        El loop continua hasta que no queden jobs pendientes o se alcance max_seconds.
        Si se alcanza el deadline, re-dispara el cron para continuar en la siguiente
        ejecución y evitar que el servidor mate el proceso.

        :param max_seconds: presupuesto de la corrida. Por defecto, la mitad del
            limite de tiempo del servidor: la otra mitad queda como margen para
            que termine el job en curso.
        """
        if max_seconds is None:
            limit = self._gq_time_limit()
            max_seconds = limit // 2 if limit else DEFAULT_RUNNER_SECONDS
        deadline = fields.Datetime.now() + timedelta(seconds=max_seconds)
        _logger.debug("GQ Job Runner started (deadline en %ds)", max_seconds)
        self._gq_recover_started_jobs()
        self._gq_release_waiting()
        job = self._gq_acquire_one_job()
        processed = 0
        while job:
            job._gq_process(commit=commit)
            processed += 1
            # Registrar avance: un runner que muere tras procesar jobs no cuenta
            # como fallo del cron. remaining=0 evita que el cron relance el
            # runner dentro de la misma pasada, fuera del presupuesto de tiempo.
            self.env["ir.cron"]._notify_progress(done=processed, remaining=0)
            if fields.Datetime.now() >= deadline:
                _logger.info(
                    "GQ Runner: deadline alcanzado (%ds), re-disparando cron", max_seconds
                )
                self._gq_cron_trigger()
                break
            self._gq_release_waiting()
            job = self._gq_acquire_one_job()
        _logger.debug("GQ Job Runner finished (%d jobs processed)", processed)

    # ── Limpieza ──────────────────────────────────────────────────────────────

    @api.autovacuum
    def _gc_finished_jobs(self):
        """Borra jobs hechos o cancelados mas antiguos que el periodo de retencion.

        Dias en el parametro ``gq_queue.keep_done_days`` (0 = no borrar).
        Los jobs fallidos se conservan para su revision.
        """
        days = int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("gq_queue.keep_done_days", DEFAULT_KEEP_DONE_DAYS)
        )
        if days <= 0:
            return
        self.env.flush_all()
        self.env.cr.execute(
            """
            DELETE FROM gq_job
            WHERE state IN ('done', 'cancelled')
              AND date_done < (now() AT TIME ZONE 'UTC') - make_interval(days => %s)
            """,
            (days,),
        )
        if self.env.cr.rowcount:
            self.invalidate_model()
            _logger.info("GQ Queue: %d job(s) terminados borrados", self.env.cr.rowcount)

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
