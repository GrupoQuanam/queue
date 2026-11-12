import json
import logging
from datetime import date, datetime

_logger = logging.getLogger(__name__)

DEFAULT_PRIORITY = 10
DEFAULT_MAX_RETRIES = 5
# Claves de contexto que se conservan al encolar
CONTEXT_KEYS = ("lang", "tz", "allowed_company_ids", "active_test")


class GQJobEncoder(json.JSONEncoder):
    """Encoder JSON que soporta tipos comunes de Odoo."""

    def default(self, obj):
        if isinstance(obj, datetime):
            return {"__gqtype__": "datetime", "value": obj.isoformat()}
        if isinstance(obj, date):
            return {"__gqtype__": "date", "value": obj.isoformat()}
        return super().default(obj)


def gq_job_decoder(dct):
    """Object hook para decodificar tipos especiales al deserializar args/kwargs."""
    if "__gqtype__" in dct:
        t = dct["__gqtype__"]
        if t == "datetime":
            return datetime.fromisoformat(dct["value"])
        if t == "date":
            return date.fromisoformat(dct["value"])
    return dct


class GQDelayable:
    """Proxy que captura la llamada a una funcion y la encola como gq.job.

    Uso::

        # Desde cualquier recordset:
        self.with_gq_delay(priority=5).my_function(arg1, kwarg=val)

        # O instanciando el proxy:
        from odoo.addons.gq_queue.delay import GQDelayable
        GQDelayable(recordset, priority=5).my_function(arg1)
    """

    def __init__(
        self,
        recordset,
        priority=DEFAULT_PRIORITY,
        eta=None,
        description=None,
        max_retries=DEFAULT_MAX_RETRIES,
        channel="root",
        identity_key=None,
        chunk_size=None,
    ):
        if identity_key and chunk_size:
            raise ValueError("identity_key y chunk_size no se pueden combinar")
        self._recordset = recordset
        self._priority = priority
        self._eta = eta
        self._description = description
        self._max_retries = max_retries
        self._channel = channel
        self._identity_key = identity_key
        self._chunk_size = chunk_size

    def _chunks(self):
        """Divide el recordset en lotes de ``chunk_size`` registros."""
        records = self._recordset
        size = self._chunk_size
        if not size or len(records) <= size:
            return [records]
        return [records[i : i + size] for i in range(0, len(records), size)]

    def __getattr__(self, name):
        # Los nombres especiales (__deepcopy__, __getstate__, ...) los consulta
        # Python sobre el proxy: no son funciones a encolar.
        if name.startswith("__"):
            raise AttributeError(name)
        recordset = self._recordset
        if not callable(getattr(type(recordset), name, None)):
            raise AttributeError(
                f"El modelo {recordset._name!r} no tiene la funcion {name!r}"
            )

        def enqueue(*args, **kwargs):
            env = recordset.env
            Job = env["gq.job"].sudo()  # encolar no requiere permisos sobre gq.job

            if self._identity_key:
                existing = Job.search(
                    [
                        ("identity_key", "=", self._identity_key),
                        ("state", "=", "pending"),
                    ],
                    limit=1,
                )
                if existing:
                    _logger.debug(
                        "GQ job #%s ya pendiente con clave %r, no se encola otro",
                        existing.id,
                        self._identity_key,
                    )
                    return existing

            description = self._description or "{}.{}({})".format(
                recordset._name,
                name,
                ", ".join(repr(a) for a in args[:2]),
            )
            context = {
                key: env.context[key] for key in CONTEXT_KEYS if key in env.context
            }
            common = {
                "user_id": env.uid,
                "company_id": env.company.id,
                "run_sudo": env.su,
                "context": json.dumps(context),
                "model_name": recordset._name,
                "method_name": name,
                "args": json.dumps(list(args), cls=GQJobEncoder),
                "kwargs": json.dumps(kwargs, cls=GQJobEncoder),
                "priority": self._priority,
                "max_retries": self._max_retries,
                "channel": self._channel or "root",
                "identity_key": self._identity_key or False,
                "state": "pending",
                "eta": self._eta or False,
            }
            chunks = self._chunks()
            vals_list = []
            for index, chunk in enumerate(chunks, start=1):
                chunk_name = description
                if len(chunks) > 1:
                    chunk_name = f"{description} [{index}/{len(chunks)}]"
                vals_list.append(
                    dict(common, name=chunk_name, record_ids=json.dumps(chunk.ids))
                )

            jobs = Job.create(vals_list)
            _logger.debug(
                "GQ enqueued job(s) %s: %s.%s", jobs.ids, recordset._name, name
            )
            return jobs

        return enqueue
