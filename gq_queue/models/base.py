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
    ):
        """Encola una llamada a metodo como gq.job para ejecucion en background.

        Retorna un GQDelayable: el metodo se encola al llamarlo sobre el proxy.

        :param priority:    Prioridad del job (menor = mas urgente). Default: 10.
        :param eta:         datetime UTC; no ejecutar antes de esta hora.
        :param description: Descripcion visible en la UI de GQ Queue.
        :param max_retries: Reintentos maximos ante GQRetryableJobError. 0 = sin limite.
        :param channel:     Canal logico (informativo). Default: 'root'.
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
        )
