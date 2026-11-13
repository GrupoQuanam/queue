import ast

from odoo import _, api, fields, models, tools
from odoo.exceptions import ValidationError


class GQJobFunction(models.Model):
    """Configuracion de los jobs de una funcion de un modelo.

    Sin registro para una funcion, sus jobs usan los valores por defecto.
    """

    _name = "gq.job.function"
    _description = "GQ Queue Job Function"
    _order = "name"

    name = fields.Char(compute="_compute_name", store=True, index=True)
    model_id = fields.Many2one(
        "ir.model", string="Model", required=True, ondelete="cascade"
    )
    method = fields.Char(string="Function", required=True)
    retry_pattern = fields.Text(
        help="Segundos de espera segun el numero de intento, ante errores "
        "reintentables que no indican su propia espera. Diccionario "
        "{intento: segundos}; un par (min, max) elige la espera al azar.\n"
        "Ejemplo: {1: 10, 5: 60, 10: 300}\n"
        "Ejemplo: {1: (1, 10), 5: (30, 60)}",
    )
    on_fail_method = fields.Char(
        string="On Fail Function",
        help="Funcion del modelo que se llama cuando el job falla y no se "
        "reintentara. Recibe exc_name, exc_message y exc_info. No debe hacer "
        "commit.",
    )

    _sql_constraints = [
        (
            "model_method_uniq",
            "unique(model_id, method)",
            "A function can only be configured once per model.",
        )
    ]

    @api.depends("model_id.model", "method")
    def _compute_name(self):
        for record in self:
            record.name = (
                f"<{record.model_id.model}>.{record.method}"
                if record.model_id and record.method
                else False
            )

    # ── Validacion ────────────────────────────────────────────────────────────

    @api.model
    def _parse_retry_pattern(self, text):
        """Convierte el texto del patron en ``{intento: segundos | (min, max)}``.

        :raises ValueError: si el texto no es un patron valido.
        """
        if not text or not text.strip():
            return {}
        try:
            pattern = ast.literal_eval(text)
        except (ValueError, SyntaxError) as err:
            raise ValueError(str(err)) from err
        if not isinstance(pattern, dict):
            raise ValueError("se espera un diccionario")
        parsed = {}
        for attempt, seconds in pattern.items():
            if isinstance(seconds, list | tuple):
                seconds = tuple(seconds)
                valid = (
                    len(seconds) == 2
                    and all(type(value) is int and value >= 0 for value in seconds)
                    and seconds[0] <= seconds[1]
                )
            else:
                valid = type(seconds) is int and seconds >= 0
            if type(attempt) is not int or attempt < 1 or not valid:
                raise ValueError(f"entrada no valida: {attempt!r}: {seconds!r}")
            parsed[attempt] = seconds
        return parsed

    @api.constrains("retry_pattern")
    def _check_retry_pattern(self):
        for record in self:
            try:
                self._parse_retry_pattern(record.retry_pattern)
            except ValueError as err:
                raise ValidationError(
                    _(
                        "Invalid retry pattern for %(name)s: %(error)s\n"
                        "Expected a dictionary like {1: 10, 5: 60} or "
                        "{1: (1, 10), 5: (30, 60)}.",
                        name=record.name,
                        error=err,
                    )
                ) from err

    @api.constrains("model_id", "method", "on_fail_method")
    def _check_functions_exist(self):
        for record in self:
            model = self.env[record.model_id.model]
            for function_name in (record.method, record.on_fail_method):
                if function_name and not callable(
                    getattr(type(model), function_name, None)
                ):
                    raise ValidationError(
                        _(
                            "Model %(model)s has no function %(function)s.",
                            model=record.model_id.model,
                            function=function_name,
                        )
                    )

    # ── Lectura de la configuracion ───────────────────────────────────────────

    @api.model
    @tools.ormcache("model_name", "method_name")
    def job_config(self, model_name, method_name):
        """Configuracion efectiva de los jobs de una funcion.

        :returns: dict con ``retry_pattern`` (ya convertido) y
            ``on_fail_method``. No modificar: el valor se comparte por cache.
        """
        record = self.sudo().search(
            [("model_id.model", "=", model_name), ("method", "=", method_name)],
            limit=1,
        )
        return {
            "retry_pattern": self._parse_retry_pattern(record.retry_pattern),
            "on_fail_method": record.on_fail_method or False,
        }

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self.env.registry.clear_cache()
        return records

    def write(self, vals):
        res = super().write(vals)
        self.env.registry.clear_cache()
        return res

    def unlink(self):
        res = super().unlink()
        self.env.registry.clear_cache()
        return res
