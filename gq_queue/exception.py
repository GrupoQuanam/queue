class GQRetryableJobError(Exception):
    """Lanzar esta excepcion para reintentar el job mas tarde.

    Parametros opcionales:
        msg     -- mensaje de resultado a guardar en el job
        seconds -- segundos a esperar antes de reintentar (default: 5)
    """

    def __init__(self, msg="", seconds=None):
        self.seconds = seconds
        super().__init__(msg)


class GQFailedJobError(Exception):
    """Lanzar esta excepcion para marcar el job como fallido sin reintentar."""


class GQNothingToDoJob(Exception):
    """Lanzar esta excepcion para marcar el job como completado sin hacer nada."""
