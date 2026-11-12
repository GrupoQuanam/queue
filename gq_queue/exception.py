class GQRetryableJobError(Exception):
    """Lanzar esta excepcion para reintentar el job mas tarde.

    Parametros opcionales:
        msg          -- mensaje de resultado a guardar en el job
        seconds      -- segundos a esperar antes de reintentar. Si se omite, se
                        usa el patron de reintentos configurado para la funcion
                        y, si no hay, 5 segundos.
        ignore_retry -- si es True, el reintento no consume un intento del job.
                        El job puede reintentarse sin limite: usar solo cuando
                        la condicion de espera termina con seguridad.
    """

    def __init__(self, msg="", seconds=None, ignore_retry=False):
        self.seconds = seconds
        self.ignore_retry = ignore_retry
        super().__init__(msg)


class GQFailedJobError(Exception):
    """Lanzar esta excepcion para marcar el job como fallido sin reintentar."""


class GQNothingToDoJob(Exception):
    """Lanzar esta excepcion para marcar el job como completado sin hacer nada."""
