from odoo import models

from ..delay import DEFAULT_MAX_RETRIES, DEFAULT_PRIORITY, GQDelayable


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
        channel="root",
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
        :param channel:     Canal logico (informativo). Default: 'root'.
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
            channel=channel,
            identity_key=identity_key,
            chunk_size=chunk_size,
            group=group,
            depends_on=depends_on,
        )
