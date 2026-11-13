import functools

from odoo import api, models

from ..delay import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_PRIORITY,
    GQDeferred,
    GQDelayable,
    gq_no_delay,
)


class Base(models.AbstractModel):
    """Extiende el modelo base de Odoo para que todos los modelos
    tengan disponible with_gq_delay() sin necesidad de heredar un mixin.
    """

    _inherit = "base"

    def with_gq_delay(
        self,
        priority=DEFAULT_PRIORITY,
        eta=None,
        description=None,
        max_retries=DEFAULT_MAX_RETRIES,
        identity_key=None,
        chunk_size=None,
        group=None,
        depends_on=None,
    ):
        """Encola una llamada a metodo como gq.job para ejecucion en background.

        Retorna un GQDelayable: el metodo se encola al llamarlo sobre el proxy.

        :param priority:    Prioridad del job (menor = mas urgente). Default: 10.
        :param eta:         datetime UTC; no ejecutar antes de esta hora.
        :param description: Descripcion visible en la UI de GQ Queue.
        :param max_retries: Reintentos maximos ante GQRetryableJobError. 0 = sin limite.
        :param identity_key: Si ya hay un job pendiente con esta clave, no se
                            encola otro y se retorna el existente.
        :param chunk_size:  Divide el recordset en lotes de este tamano y
                            encola un job por lote.
        :param group:       Identificador de grupo. Si un job del grupo falla,
                            los demas no se ejecutan hasta reencolarlo. Los
                            lotes de ``chunk_size`` forman un grupo solos.
        :param depends_on:  Recordset gq.job que debe terminar antes. El job
                            nuevo se une al grupo de esos jobs.
        :returns: GQDelayable

        Con ``gq_queue__no_delay`` en el contexto (o la variable de entorno
        ``GQ_QUEUE__NO_DELAY``) la funcion se ejecuta en linea y se retorna su
        resultado en lugar del gq.job.

        Ejemplo::

            # Desde cualquier modelo, sin necesidad de mixin:
            self.with_gq_delay(priority=5).mi_metodo(arg1, kwarg=val)

            # Con ETA:
            from datetime import datetime, timedelta
            eta = datetime.utcnow() + timedelta(hours=1)
            self.with_gq_delay(eta=eta).mi_metodo()
        """
        return GQDelayable(
            self,
            priority=priority,
            eta=eta,
            description=description,
            max_retries=max_retries,
            identity_key=identity_key,
            chunk_size=chunk_size,
            group=group,
            depends_on=depends_on,
        )

    def gq_delayable(
        self,
        priority=DEFAULT_PRIORITY,
        eta=None,
        description=None,
        max_retries=DEFAULT_MAX_RETRIES,
        identity_key=None,
        chunk_size=None,
        group=None,
        depends_on=None,
    ):
        """Prepara una llamada diferida para armar grafos de jobs.

        A diferencia de ``with_gq_delay()``, la funcion no se encola al
        llamarla: se encola con ``delay()``, junto con el resto del grafo.
        Acepta las mismas opciones que ``with_gq_delay()``.

        Ejemplo::

            from odoo.addons.gq_queue.delay import gq_chain, gq_group

            gq_chain(
                self.gq_delayable()._exportar(),
                gq_group(
                    lote1.gq_delayable()._enviar(),
                    lote2.gq_delayable()._enviar(),
                ),
                self.gq_delayable(priority=1)._archivar(),
            ).delay()

            # Equivalente con on_done:
            self.gq_delayable()._exportar().on_done(
                self.gq_delayable()._archivar()
            ).delay()

        :returns: GQDeferred
        """
        return GQDeferred(
            self,
            priority=priority,
            eta=eta,
            description=description,
            max_retries=max_retries,
            identity_key=identity_key,
            chunk_size=chunk_size,
            group=group,
            depends_on=depends_on,
        )

    # ── Encolado automatico de funciones existentes ───────────────────────────

    def _patch_gq_job_auto_delay(self, method_name, context_key=None):
        """Hace que una funcion existente se encole sola al ser llamada.

        Sirve para volver asincrona una funcion que otro codigo llama de forma
        sincrona, sin modificar a quien la llama. Se usa en ``_register_hook``.

        Con ``context_key``, la funcion solo se encola cuando esa clave esta en
        el contexto de quien la llama. Es lo recomendado: encolar siempre puede
        romper a quien espera el resultado o el efecto inmediato de la funcion.

        Las opciones del job (priority, description, identity_key, ...) las
        puede devolver una funcion con el mismo nombre y sufijo
        ``_gq_job_options``, que recibe los mismos argumentos.

        Ejemplo::

            def _generar_pdf(self, plantilla):
                ...

            def _generar_pdf_gq_job_options(self, plantilla):
                return {"priority": 20, "description": f"PDF {plantilla}"}

            def _register_hook(self):
                self._gq_patch_method(
                    "_generar_pdf",
                    self._patch_gq_job_auto_delay(
                        "_generar_pdf", context_key="gq_auto_delay_pdf"
                    ),
                )
                return super()._register_hook()

            # Quien quiere el comportamiento asincrono:
            self.with_context(gq_auto_delay_pdf=True).accion_que_genera_pdf()

        Dentro de un job (``job_uuid`` en el contexto) y en modo sin encolado,
        la funcion se ejecuta directamente.
        """

        def auto_delay_wrapper(self, *args, **kwargs):
            context_delay = self.env.context.get(context_key) if context_key else True
            if (
                self.env.context.get("job_uuid")
                or not context_delay
                or gq_no_delay(self.env)
            ):
                return auto_delay_wrapper.origin(self, *args, **kwargs)
            name = auto_delay_wrapper.origin.__name__
            options_function = getattr(self, f"{name}_gq_job_options", None)
            options = options_function(*args, **kwargs) if options_function else {}
            return getattr(self.with_gq_delay(**options), name)(*args, **kwargs)

        origin = getattr(self, method_name)
        return functools.update_wrapper(auto_delay_wrapper, origin)

    @classmethod
    def _gq_patch_method(cls, name, method):
        """Reemplaza la funcion ``name`` de la clase, conservando la original
        en ``method.origin`` y sus decoradores de api."""
        origin = getattr(cls, name)
        method.origin = origin
        wrapped = api.propagate(origin, method)
        wrapped.origin = origin
        setattr(cls, name, wrapped)
