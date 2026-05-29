import json
import logging
from datetime import date, datetime

_logger = logging.getLogger(__name__)

DEFAULT_PRIORITY = 10
DEFAULT_MAX_RETRIES = 5


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
    """Proxy que captura llamadas a metodos y las encola como gq.job.

    Uso::

        # En un modelo que hereda gq.job.mixin:
        self.with_gq_delay(priority=5).my_method(arg1, kwarg=val)

        # O directamente desde cualquier recordset:
        from odoo.addons.gq_queue.delay import GQDelayable
        GQDelayable(recordset, priority=5).my_method(arg1)
    """

    def __init__(
        self,
        recordset,
        priority=DEFAULT_PRIORITY,
        eta=None,
        description=None,
        max_retries=DEFAULT_MAX_RETRIES,
        channel="root",
    ):
        self._recordset = recordset
        self._priority = priority
        self._eta = eta
        self._description = description
        self._max_retries = max_retries
        self._channel = channel

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def enqueue(*args, **kwargs):
            env = self._recordset.env
            model_name = self._recordset._name
            record_ids = self._recordset.ids

            description = self._description or "{}.{}({})".format(
                model_name,
                name,
                ", ".join(repr(a) for a in args[:2]),
            )

            vals = {
                "name": description,
                "model_name": model_name,
                "method_name": name,
                "record_ids": json.dumps(record_ids),
                "args": json.dumps(list(args), cls=GQJobEncoder),
                "kwargs": json.dumps(kwargs, cls=GQJobEncoder),
                "priority": self._priority,
                "max_retries": self._max_retries,
                "channel": self._channel or "root",
                "state": "pending",
            }
            if self._eta:
                vals["eta"] = self._eta

            job = env["gq.job"].create(vals)
            _logger.debug(
                "GQ enqueued job #%s: %s.%s", job.id, model_name, name
            )
            return job

        return enqueue
