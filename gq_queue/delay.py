import itertools
import json
import logging
import os
import uuid
from datetime import date, datetime

from odoo import models
from odoo.tools import str2bool

_logger = logging.getLogger(__name__)

DEFAULT_PRIORITY = 10
DEFAULT_MAX_RETRIES = 5
# Claves de contexto que se conservan al encolar
CONTEXT_KEYS = ("lang", "tz", "allowed_company_ids", "active_test")

OPTION_NAMES = (
    "priority",
    "eta",
    "description",
    "max_retries",
    "identity_key",
    "chunk_size",
    "group",
    "depends_on",
)
OPTION_DEFAULTS = {"priority": DEFAULT_PRIORITY, "max_retries": DEFAULT_MAX_RETRIES}

# Clave de contexto y variable de entorno para ejecutar en linea, sin encolar
NO_DELAY_CONTEXT_KEY = "gq_queue__no_delay"
NO_DELAY_ENV_VAR = "GQ_QUEUE__NO_DELAY"


class GQJobEncoder(json.JSONEncoder):
    """Encoder JSON que soporta tipos comunes de Odoo."""

    def default(self, obj):
        if isinstance(obj, models.BaseModel):
            return {
                "__gqtype__": "recordset",
                "model": obj._name,
                "ids": obj.ids,
                "su": obj.env.su,
            }
        if isinstance(obj, datetime):
            return {"__gqtype__": "datetime", "value": obj.isoformat()}
        if isinstance(obj, date):
            return {"__gqtype__": "date", "value": obj.isoformat()}
        return super().default(obj)


def gq_job_decoder(dct, env):
    """Object hook para decodificar tipos especiales al deserializar args/kwargs.

    :param env: entorno del job, necesario para reconstruir recordsets.
    """
    if "__gqtype__" in dct:
        t = dct["__gqtype__"]
        if t == "recordset":
            records = env[dct["model"]].browse(dct["ids"])
            return records.sudo() if dct.get("su") else records
        if t == "datetime":
            return datetime.fromisoformat(dct["value"])
        if t == "date":
            return date.fromisoformat(dct["value"])
    return dct


def gq_no_delay(env):
    """True si las funciones encoladas deben ejecutarse en linea.

    Se activa con ``gq_queue__no_delay`` en el contexto o con la variable de
    entorno ``GQ_QUEUE__NO_DELAY``. Pensado para tests y depuracion.
    """
    if env.context.get(NO_DELAY_CONTEXT_KEY):
        return True
    return str2bool(os.environ.get(NO_DELAY_ENV_VAR, ""), default=False)


def _check_options(options):
    unknown = set(options) - set(OPTION_NAMES)
    if unknown:
        raise TypeError(f"Opciones de job no validas: {sorted(unknown)}")
    if options.get("identity_key") and options.get("chunk_size"):
        raise ValueError("identity_key y chunk_size no se pueden combinar")


def _check_function(recordset, name):
    """Valida que ``name`` sea una funcion encolable del modelo."""
    # Los nombres especiales (__deepcopy__, __getstate__, ...) los consulta
    # Python sobre el proxy: no son funciones a encolar.
    if name.startswith("__"):
        raise AttributeError(name)
    if not callable(getattr(type(recordset), name, None)):
        raise AttributeError(
            f"El modelo {recordset._name!r} no tiene la funcion {name!r}"
        )


def _release(Job):
    """Libera los jobs en espera que ya pueden ejecutarse y despierta al runner."""
    Job._gq_release_waiting()
    # Un job que sigue en espera tambien despierta al runner: si su dependencia
    # termino mientras se encolaba, el runner lo libera.
    Job._gq_cron_trigger()


class GQCall:
    """Llamada a una funcion de un recordset, lista para encolar o ejecutar."""

    def __init__(self, recordset, name, args, kwargs, options):
        self.recordset = recordset
        self.name = name
        self.args = args
        self.kwargs = kwargs
        self.options = dict(OPTION_DEFAULTS, **options)

    def _chunks(self):
        """Divide el recordset en lotes de ``chunk_size`` registros."""
        records = self.recordset
        size = self.options.get("chunk_size")
        if not size or len(records) <= size:
            return [records]
        return [records[i : i + size] for i in range(0, len(records), size)]

    def _group(self, depends_on, chunk_count, default_group):
        """Grupo de los jobs a crear.

        Por orden: el indicado en ``group``, el del grafo al que pertenece la
        llamada, el de las dependencias (si no tienen, se crea uno y se les
        asigna) o uno nuevo para varios lotes.
        """
        if self.options.get("group"):
            return self.options["group"]
        if default_group:
            return default_group
        if depends_on:
            groups = set(depends_on.mapped("group_uuid"))
            if len(groups) > 1:
                # Dependencias de grupos distintos: el job no se une a ninguno
                return None
            group = groups.pop() or str(uuid.uuid4())
            depends_on.filtered(lambda job: not job.group_uuid).group_uuid = group
            return group
        if chunk_count > 1:
            return str(uuid.uuid4())
        return None

    def run_direct(self):
        """Ejecuta la funcion en linea, un lote a la vez.

        :returns: el resultado de la funcion, o la lista de resultados si
            ``chunk_size`` genera varios lotes.
        """
        results = [
            getattr(chunk, self.name)(*self.args, **self.kwargs)
            for chunk in self._chunks()
        ]
        return results[0] if len(results) == 1 else results

    def enqueue(self, extra_depends_on=None, default_group=None, release=True):
        """Crea los gq.job de la llamada.

        :param extra_depends_on: jobs de los que depende por su lugar en un grafo.
        :param default_group: grupo del grafo, si la llamada no indica uno.
        :param release: liberar los jobs en espera y despertar al runner. Un
            grafo lo hace una sola vez al terminar de encolar todos sus jobs.
        :returns: recordset gq.job.
        """
        recordset = self.recordset
        options = self.options
        env = recordset.env
        Job = env["gq.job"].sudo()  # encolar no requiere permisos sobre gq.job

        identity_key = options.get("identity_key")
        if identity_key:
            existing = Job.search(
                [
                    ("identity_key", "=", identity_key),
                    ("state", "in", ("waiting", "pending")),
                ],
                limit=1,
            )
            if existing:
                _logger.debug(
                    "GQ job #%s ya pendiente con clave %r, no se encola otro",
                    existing.id,
                    identity_key,
                )
                return existing

        description = options.get("description") or "{}.{}({})".format(
            recordset._name,
            self.name,
            ", ".join(repr(a) for a in self.args[:2]),
        )
        context = {key: env.context[key] for key in CONTEXT_KEYS if key in env.context}
        common = {
            "user_id": env.uid,
            "company_id": env.company.id,
            "run_sudo": env.su,
            "context": json.dumps(context),
            "model_name": recordset._name,
            "method_name": self.name,
            "args": json.dumps(list(self.args), cls=GQJobEncoder),
            "kwargs": json.dumps(self.kwargs, cls=GQJobEncoder),
            "priority": options["priority"],
            "max_retries": options["max_retries"],
            "identity_key": identity_key or False,
            "state": "pending",
            "eta": options.get("eta") or False,
        }
        chunks = self._chunks()
        depends_on = Job.browse(
            options["depends_on"].ids if options.get("depends_on") else []
        )
        if extra_depends_on:
            depends_on |= extra_depends_on
        group = self._group(depends_on, len(chunks), default_group)
        waiting = bool(group or depends_on)
        if waiting:
            # Nace en espera: _gq_release_waiting lo pasa a 'pending' si sus
            # dependencias estan hechas y su grupo no tiene un job fallido.
            common.update(
                state="waiting",
                group_uuid=group or False,
                depends_on_ids=[(6, 0, depends_on.ids)],
            )
        vals_list = []
        for index, chunk in enumerate(chunks, start=1):
            chunk_name = description
            if len(chunks) > 1:
                chunk_name = f"{description} [{index}/{len(chunks)}]"
            vals_list.append(
                dict(common, name=chunk_name, record_ids=json.dumps(chunk.ids))
            )

        jobs = Job.create(vals_list)
        if waiting and release:
            _release(Job)
        _logger.debug(
            "GQ enqueued job(s) %s: %s.%s", jobs.ids, recordset._name, self.name
        )
        return jobs


class GQDelayable:
    """Proxy que encola la funcion en el momento de llamarla.

    Uso::

        self.with_gq_delay(priority=5).my_function(arg1, kwarg=val)
    """

    def __init__(self, recordset, **options):
        _check_options(options)
        self._recordset = recordset
        self._options = options

    def __getattr__(self, name):
        _check_function(self._recordset, name)

        def enqueue(*args, **kwargs):
            call = GQCall(self._recordset, name, args, kwargs, self._options)
            if gq_no_delay(self._recordset.env):
                return call.run_direct()
            return call.enqueue()

        return enqueue


# ── Grafos de jobs ────────────────────────────────────────────────────────────


def _link(parents, children):
    for parent in parents:
        for child in children:
            parent._children.add(child)
            child._parents.add(parent)


def _topological_order(start):
    """Llamadas diferidas conectadas a ``start``, dependencias primero."""
    component = []
    pending = [start]
    while pending:
        deferred = pending.pop()
        if deferred in component:
            continue
        component.append(deferred)
        pending.extend(deferred._parents | deferred._children)
    # Orden estable: el de creacion de las llamadas
    component.sort(key=lambda deferred: deferred._sequence)

    ordered = []
    remaining = {deferred: set(deferred._parents) for deferred in component}
    while remaining:
        ready = [deferred for deferred in remaining if not remaining[deferred]]
        if not ready:
            raise ValueError("El grafo de jobs tiene una dependencia circular")
        for deferred in ready:
            ordered.append(deferred)
            del remaining[deferred]
        for parents in remaining.values():
            parents.difference_update(ready)
    return ordered


class GQGraphNode:
    """Parte de un grafo de jobs: una llamada diferida, un grupo o una cadena."""

    def _heads(self):
        """Llamadas diferidas por las que empieza este nodo."""
        raise NotImplementedError

    def _tails(self):
        """Llamadas diferidas con las que termina este nodo."""
        raise NotImplementedError

    def on_done(self, *nodes):
        """Ejecuta ``nodes`` cuando este nodo termino correctamente.

        :returns: este mismo nodo, para encadenar ``.delay()``.
        """
        for node in nodes:
            _link(self._tails(), node._heads())
        return self

    def delay(self):
        """Encola todos los jobs del grafo al que pertenece este nodo.

        Los jobs comparten grupo: si uno falla, los demas no se ejecutan.

        :returns: recordset gq.job con los jobs creados, en orden de ejecucion.
            En modo sin encolado (``gq_no_delay``) las funciones se ejecutan en
            ese mismo orden y se retorna None.
        """
        deferreds = _topological_order(self._heads()[0])
        env = deferreds[0]._recordset.env
        if gq_no_delay(env):
            for deferred in deferreds:
                deferred._call().run_direct()
            return None

        Job = env["gq.job"].sudo()
        group = str(uuid.uuid4()) if len(deferreds) > 1 else None
        jobs_by_node = {}
        jobs = Job
        for deferred in deferreds:
            parent_jobs = Job
            for parent in deferred._parents:
                parent_jobs |= jobs_by_node[parent]
            node_jobs = deferred._call().enqueue(
                extra_depends_on=parent_jobs, default_group=group, release=False
            )
            jobs_by_node[deferred] = node_jobs
            jobs |= node_jobs
        _release(Job)
        return jobs


class GQDeferred(GQGraphNode):
    """Llamada diferida: se encola al llamar ``delay()``, no al invocarla.

    Uso::

        paso1 = self.gq_delayable().exportar()
        paso2 = self.gq_delayable(priority=5).enviar(destino)
        paso1.on_done(paso2).delay()
    """

    _counter = itertools.count()

    def __init__(self, recordset, **options):
        _check_options(options)
        self._recordset = recordset
        self._options = options
        self._bound = None
        self._parents = set()
        self._children = set()
        self._sequence = next(self._counter)

    def set(self, **options):
        """Cambia opciones del job: priority, eta, description, max_retries..."""
        _check_options(dict(self._options, **options))
        self._options.update(options)
        return self

    def __getattr__(self, name):
        _check_function(self._recordset, name)
        if self._bound:
            raise ValueError(
                f"Esta llamada diferida ya tiene la funcion {self._bound[0]!r}"
            )

        def bind(*args, **kwargs):
            self._bound = (name, args, kwargs)
            return self

        return bind

    def _call(self):
        if not self._bound:
            raise ValueError(
                f"Llamada diferida sobre {self._recordset._name!r} sin funcion"
            )
        name, args, kwargs = self._bound
        return GQCall(self._recordset, name, args, kwargs, self._options)

    def _heads(self):
        return [self]

    def _tails(self):
        return [self]


class GQGroup(GQGraphNode):
    """Nodos sin orden entre si: pueden ejecutarse en paralelo."""

    def __init__(self, *nodes):
        if not nodes:
            raise ValueError("Un grupo necesita al menos un nodo")
        self._nodes = nodes

    def _heads(self):
        return [head for node in self._nodes for head in node._heads()]

    def _tails(self):
        return [tail for node in self._nodes for tail in node._tails()]


class GQChain(GQGraphNode):
    """Nodos en secuencia: cada uno espera a que termine el anterior."""

    def __init__(self, *nodes):
        if not nodes:
            raise ValueError("Una cadena necesita al menos un nodo")
        self._nodes = nodes
        for previous, following in itertools.pairwise(nodes):
            _link(previous._tails(), following._heads())

    def _heads(self):
        return self._nodes[0]._heads()

    def _tails(self):
        return self._nodes[-1]._tails()


def gq_group(*nodes):
    """Agrupa nodos que pueden ejecutarse en paralelo.

    Uso::

        gq_group(
            lote1.gq_delayable().procesar(),
            lote2.gq_delayable().procesar(),
        ).on_done(self.gq_delayable().notificar_fin()).delay()
    """
    return GQGroup(*nodes)


def gq_chain(*nodes):
    """Encadena nodos para ejecutarlos en orden.

    Uso::

        gq_chain(
            self.gq_delayable().exportar(),
            self.gq_delayable().enviar(),
            self.gq_delayable().archivar(),
        ).delay()
    """
    return GQChain(*nodes)
