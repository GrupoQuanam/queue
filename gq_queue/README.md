# GQ Queue

Módulo Odoo 16 para ejecución asíncrona de tareas en segundo plano mediante CronWorkers de Odoo. No requiere proceso externo ni depende de `queue_job`.

---

## Índice

1. [Arquitectura general](#1-arquitectura-general)
2. [Instalación y dependencias](#2-instalación-y-dependencias)
3. [Estructura de archivos](#3-estructura-de-archivos)
4. [Modelo `gq.job`](#4-modelo-gqjob)
5. [Ciclo de vida de un job](#5-ciclo-de-vida-de-un-job)
6. [Cómo encolar un job](#6-cómo-encolar-un-job)
7. [Disponibilidad de `with_gq_delay()`](#7-disponibilidad-de-with_gq_delay)
8. [Serialización de argumentos](#8-serialización-de-argumentos)
9. [Manejo de errores y excepciones](#9-manejo-de-errores-y-excepciones)
10. [Runner y sistema de triggers](#10-runner-y-sistema-de-triggers)
11. [Concurrencia y paralelismo](#11-concurrencia-y-paralelismo)
12. [Configuración de Odoo](#12-configuración-de-odoo)
13. [Referencia de API](#13-referencia-de-api)
14. [Limitaciones conocidas](#14-limitaciones-conocidas)

---

## 1. Arquitectura general

```
┌─────────────────────────────────────────────────────────────────┐
│  Tu módulo custom                                               │
│                                                                 │
│  record.with_gq_delay(priority=5).mi_metodo(arg)               │
│          │                                                      │
│          ▼                                                      │
│   GQDelayable.__getattr__  ──→  gq.job.create(...)             │
└──────────────────────────────────────┬──────────────────────────┘
                                       │ ORM hook create()
                                       ▼
                             _gq_ensure_cron_trigger()
                                       │
                                       ▼
                             ir.cron._trigger()  ←── despierta el CronWorker
                                       │
                    ┌──────────────────┘
                    ▼
          _gq_job_runner()  [loop]
                    │
                    ├── _gq_acquire_one_job()   ← SELECT ... FOR NO KEY UPDATE SKIP LOCKED
                    │
                    └── _gq_process()
                              │
                              ├── savepoint → _gq_perform()   ← ejecuta model.method(*args)
                              │
                              └── manejo de errores → actualiza state
```

**Principios de diseño:**
- **Sin proceso externo:** todo corre dentro de los CronWorkers de Odoo.
- **A demanda:** el cron se despierta solo cuando hay jobs pendientes.
- **Aislado:** cada job corre en su propio `savepoint`; un fallo no afecta a otros.
- **Sin colisiones:** PostgreSQL `FOR NO KEY UPDATE SKIP LOCKED` garantiza que dos workers nunca tomen el mismo job.

---

## 2. Instalación y dependencias

**Dependencias Odoo:** solo `base`.

**Dependencias Python:** `psycopg2` (incluida en cualquier instalación estándar de Odoo).

Agregar `gq_queue` al `depends` de tu módulo custom:

```python
# my_module/__manifest__.py
{
    "depends": ["base", "gq_queue"],
}
```

---

## 3. Estructura de archivos

```
gq_queue/
├── __init__.py
├── __manifest__.py
├── exception.py               # GQRetryableJobError, GQFailedJobError, GQNothingToDoJob
├── delay.py                   # GQDelayable, GQJobEncoder, gq_job_decoder
├── models/
│   ├── __init__.py
│   ├── base.py                # Extiende 'base': agrega with_gq_delay() a todos los modelos
│   ├── gq_job.py              # Modelo gq.job + runner completo
│   └── ir_cron.py             # Agrega campo gq_job_runner a ir.cron
├── security/
│   └── ir.model.access.csv
├── data/
│   └── ir_cron.xml            # Cron "GQ Queue Job Runner"
└── views/
    ├── gq_job_views.xml       # Vistas tree, form, search y menú
    └── ir_cron_views.xml      # Agrega campo gq_job_runner al form de ir.cron
```

---

## 4. Modelo `gq.job`

Tabla PostgreSQL: `gq_job`

### Campos

| Campo | Tipo | Descripción |
|---|---|---|
| `name` | Char | Descripción legible del job (auto-generada o personalizada) |
| `uuid` | Char | Identificador único (UUID4), indexado |
| `model_name` | Char | Nombre técnico del modelo Odoo, p.ej. `sale.order` |
| `method_name` | Char | Nombre del método a ejecutar, p.ej. `action_confirm` |
| `record_ids` | Text | JSON array con los IDs del recordset (`[1, 2, 3]`) |
| `args` | Text | JSON array con argumentos posicionales |
| `kwargs` | Text | JSON object con argumentos nombrados |
| `state` | Selection | Estado actual del job (ver sección 5) |
| `priority` | Integer | Prioridad de ejecución. Menor = más urgente. Default: `10` |
| `eta` | Datetime | No ejecutar antes de esta fecha/hora UTC |
| `max_retries` | Integer | Máximo de reintentos en error retryable. `0` = sin límite. Default: `5` |
| `retry_count` | Integer | Número de reintentos realizados hasta ahora |
| `channel` | Char | Canal lógico (informativo). Default: `"root"` |
| `result` | Text | Mensaje de resultado (éxito o descripción del reintento) |
| `exc_info` | Text | Traceback completo en caso de fallo |
| `date_created` | Datetime | Fecha de creación del job |
| `date_started` | Datetime | Fecha en que el runner comenzó a ejecutarlo |
| `date_done` | Datetime | Fecha de finalización (done, failed o cancelled) |

### Orden de procesamiento

Los jobs se toman de la cola ordenados por:
1. `priority ASC` — menor número primero
2. `date_created ASC` — FIFO dentro de la misma prioridad

---

## 5. Ciclo de vida de un job

```
                         GQRetryableJobError
                         (mientras _gq_can_retry())
                    ┌────────────────────────────────┐
                    │    + ETA (segundos de espera)  │
                    ▼                                │
  [pending] ──→ [started] ──────────────────────────┘
                    │
                    ├──→ [done]       Ejecución exitosa
                    │                o GQNothingToDoJob
                    │
                    ├──→ [failed]     GQFailedJobError
                    │                o Exception no controlada
                    │                o GQRetryableJobError con max_retries agotado
                    │
                    └──→ [cancelled]  Cancelación manual
```

### Descripción de estados

| Estado | Descripción |
|---|---|
| `pending` | Esperando ser tomado por el runner. Estado inicial. |
| `started` | Runner lo tomó y está ejecutando el método. |
| `done` | Método ejecutado correctamente. |
| `failed` | El job falló definitivamente (ver `exc_info`). |
| `cancelled` | Cancelado manualmente mediante `_gq_set_cancelled()`. |

---

## 6. Cómo encolar un job

`with_gq_delay()` está disponible en **todos los modelos** de Odoo sin necesidad de heredar nada. Basta con que `gq_queue` esté instalado.

```python
# Ejecución inmediata en background
self.with_gq_delay().process_heavy_task(date.today())

# Con prioridad alta (número bajo = más urgente)
self.with_gq_delay(priority=1).process_heavy_task(date.today())

# Con descripción personalizada visible en la UI
self.with_gq_delay(description="Procesar pedido #123").process_heavy_task(date.today())

# Con ETA: ejecutar dentro de 2 horas
from datetime import datetime, timedelta
eta = datetime.utcnow() + timedelta(hours=2)
self.with_gq_delay(eta=eta).process_heavy_task(date.today())

# Configurando reintentos máximos
self.with_gq_delay(max_retries=3).process_heavy_task(date.today())

# Sin límite de reintentos
self.with_gq_delay(max_retries=0).process_heavy_task(date.today())
```

También funciona desde `env` sobre cualquier modelo:

```python
# Sobre un recordset obtenido desde env
self.env["sale.order"].browse([1, 2, 3]).with_gq_delay(priority=5).action_confirm()

# Sobre el modelo directamente (para métodos @api.model)
self.env["my.model"].with_gq_delay().some_model_method(param)
```

### Parámetros de `with_gq_delay()`

| Parámetro | Tipo | Default | Descripción |
|---|---|---|---|
| `priority` | int | `10` | Prioridad de ejecución. Menor = más urgente. |
| `eta` | datetime | `None` | No ejecutar antes de esta fecha/hora (UTC). |
| `description` | str | Auto | Descripción del job visible en la UI. |
| `max_retries` | int | `5` | Reintentos máximos ante `GQRetryableJobError`. `0` = sin límite. |
| `channel` | str | `"root"` | Canal lógico del job (informativo). |

---

## 7. Disponibilidad de `with_gq_delay()`

El módulo extiende el modelo `base` de Odoo (`models/base.py`), lo que hace que `with_gq_delay()` esté disponible en **todos los modelos** sin necesidad de heredar ningún mixin.

```python
# Funciona en cualquier modelo sin configuración adicional:
self.env["sale.order"].browse([1]).with_gq_delay().action_confirm()
self.env["res.partner"].browse([5]).with_gq_delay().send_welcome_email()
self.env["account.move"].browse([10]).with_gq_delay(priority=1).action_post()
```

Este es el mismo patrón que usa Odoo internamente para `mail.thread`, `mail.activity.mixin` y similares. Agregar un método a `base` tiene **impacto nulo en rendimiento** porque:

- No agrega columnas a ninguna tabla de BD.
- No interviene en `create`, `write`, `read` ni `search`.
- El método solo ejecuta código cuando es invocado explícitamente.
- La resolución MRO ocurre una sola vez al arrancar Odoo.

---

## 8. Serialización de argumentos

Los argumentos del método se serializan a JSON al crear el job y se deserializan al ejecutarlo.

### Tipos soportados nativamente por JSON

`str`, `int`, `float`, `bool`, `None`, `list`, `dict`

### Tipos especiales (via `GQJobEncoder` / `gq_job_decoder`)

| Tipo Python | Representación en JSON |
|---|---|
| `datetime` | `{"__gqtype__": "datetime", "value": "2026-05-15T10:30:00"}` |
| `date` | `{"__gqtype__": "date", "value": "2026-05-15"}` |

### Tipos NO soportados

Los recordsets de Odoo no se serializan automáticamente. Si necesitas pasar un recordset como argumento, usa sus IDs:

```python
# ❌ No funciona
record.with_gq_delay().mi_metodo(other_record)

# ✅ Correcto: pasar IDs y re-browse dentro del método
record.with_gq_delay().mi_metodo(other_record.ids)

def mi_metodo(self, other_ids):
    other = self.env["other.model"].browse(other_ids)
```

---

## 9. Manejo de errores y excepciones

Importar desde `odoo.addons.gq_queue.exception`:

```python
from odoo.addons.gq_queue.exception import (
    GQRetryableJobError,
    GQFailedJobError,
    GQNothingToDoJob,
)
```

### `GQRetryableJobError`

El job vuelve a `pending` con una ETA futura y se reintentará.

```python
def mi_metodo(self):
    if recurso_no_disponible():
        # Reintenta en 60 segundos (default: 5s)
        raise GQRetryableJobError("Recurso ocupado, reintentando...", seconds=60)

    if servicio_externo_caido():
        # Reintenta en 10 minutos
        raise GQRetryableJobError("API no disponible", seconds=600)
```

**Comportamiento:**
- Incrementa `retry_count` en cada reintento.
- Si `retry_count >= max_retries` (y `max_retries > 0`), el job pasa a `failed`.
- `seconds=None` equivale a 5 segundos.

### `GQFailedJobError`

El job pasa a `failed` inmediatamente, sin reintentar.

```python
def mi_metodo(self):
    if not self.tiene_datos_requeridos():
        raise GQFailedJobError("Faltan datos obligatorios para procesar")
```

### `GQNothingToDoJob`

El job pasa a `done` sin considerar que hubo un error real.

```python
def mi_metodo(self):
    if self.state == "done":
        raise GQNothingToDoJob("El registro ya fue procesado anteriormente")
    # procesar...
```

### Excepciones no controladas

Cualquier otra excepción (`ValueError`, `KeyError`, etc.) causa que el job pase a `failed` con el traceback completo guardado en `exc_info`.

### Errores de concurrencia PostgreSQL

Los errores de serialización de PostgreSQL (`OperationalError` con códigos en `PG_CONCURRENCY_ERRORS_TO_RETRY`) son capturados automáticamente. El job vuelve a `pending` con una ETA de 5 segundos sin consumir un reintento.

---

## 10. Runner y sistema de triggers

### Cómo se activa el runner

El cron **no corre continuamente**. Se despierta a demanda:

```
Job creado (state=pending, eta=NULL)
    │
    └── create() → _gq_ensure_cron_trigger() → _gq_cron_trigger()
                                                      │
                                               ir.cron._trigger()  ← despierta AHORA
                                                      │
                                               CronWorker libre
                                                      │
                                               _gq_job_runner()
                                                      │
                                          loop hasta vaciar la cola
                                                      │
                                               cron termina (duerme)
```

Para jobs con ETA:

```python
# El cron se programa para despertar exactamente en eta
_gq_cron_trigger(at=eta_datetime)
```

### Flujo interno de `_gq_job_runner()`

```python
def _gq_job_runner(self, commit=True):
    job = self._gq_acquire_one_job()   # SQL SELECT ... FOR NO KEY UPDATE SKIP LOCKED
    while job:
        job._gq_process(commit=commit) # ejecuta + actualiza estado
        job = self._gq_acquire_one_job()
    # termina cuando no hay más jobs pendientes
```

### Flujo interno de `_gq_process()`

```
_gq_set_started()          → state = 'started', date_started = now()
    │
    ├── savepoint
    │       ├── _gq_perform()              → ejecuta model.method(*args, **kwargs)
    │       └── _gq_set_done()            → state = 'done'
    │
    ├── OperationalError (PG concurrency) → _gq_postpone_pending(seconds=5, reset_retry=True)
    ├── GQNothingToDoJob                  → _gq_set_done(result=msg)
    ├── GQRetryableJobError               → _gq_postpone_pending() o _gq_set_failed()
    └── GQFailedJobError / Exception      → _gq_set_failed(exc_info=traceback)
```

---

## 11. Concurrencia y paralelismo

### Bloqueo PostgreSQL

El método `_gq_acquire_one_job()` usa:

```sql
SELECT id FROM gq_job
WHERE state = 'pending'
  AND (eta IS NULL OR eta <= (now() AT TIME ZONE 'UTC'))
ORDER BY priority ASC, date_created ASC
LIMIT 1 FOR NO KEY UPDATE SKIP LOCKED
```

- **`FOR NO KEY UPDATE`**: bloquea la fila seleccionada para el resto de la transacción.
- **`SKIP LOCKED`**: si la fila ya está bloqueada por otro worker, la salta y busca la siguiente.

Esto garantiza que dos CronWorkers nunca procesen el mismo job.

### Configurar ejecución paralela

Por defecto hay **1 cron → 1 worker**. Para N workers en paralelo:

**Paso 1:** Duplicar el cron en XML (o desde `Ajustes → Técnico → Acciones planificadas`):

```xml
<!-- En el XML de tu módulo -->
<record id="gq_job_cron_2" model="ir.cron">
    <field name="name">GQ Queue Job Runner 2</field>
    <field name="model_id" ref="gq_queue.model_gq_job"/>
    <field name="state">code</field>
    <field name="code">model._gq_job_runner()</field>
    <field name="gq_job_runner" eval="True"/>
    <field name="user_id" ref="base.user_root"/>
    <field name="interval_number">1</field>
    <field name="interval_type">days</field>
    <field name="numbercall">-1</field>
</record>
```

**Paso 2:** Configurar Odoo para usar suficientes CronWorkers:

```ini
# odoo.cfg
max_cron_threads = 2
```

Con N crons y `max_cron_threads = N`, todos los crons se disparan simultáneamente y cada worker toma jobs diferentes.

---

## 12. Configuración de Odoo

### `odoo.cfg` recomendado

```ini
[options]
# Número de CronWorkers disponibles (debe ser >= número de crons gq_job_runner)
max_cron_threads = 2

# Opcional: deshabilitar timeout de CPU para CronWorkers
# Evita que jobs largos sean interrumpidos abruptamente
# limit_time_real_cron = 0   # En Odoo.sh esto ya está configurado por defecto
```

### Variables de entorno

No se requieren variables de entorno adicionales.

---

## 13. Referencia de API

### `gq.job` — métodos públicos relevantes

| Método | Descripción |
|---|---|
| `_gq_set_cancelled()` | Cancela un job manualmente (state → cancelled) |
| `_gq_job_runner(commit=True)` | Entry point del cron. Procesa toda la cola. |
| `_gq_cron_trigger(at=None)` | Dispara todos los crons `gq_job_runner`. |

### `GQDelayable` — `delay.py`

Clase proxy usada internamente por `with_gq_delay()`. También puede instanciarse directamente para uso avanzado:

```python
from odoo.addons.gq_queue.delay import GQDelayable

GQDelayable(recordset, priority=10, eta=None, description=None, max_retries=5, channel="root")
```

Al llamar cualquier método sobre el proxy, crea y retorna el `gq.job`:

```python
job = self.env["sale.order"].browse([1]).with_gq_delay().action_confirm()
# job es el registro gq.job creado (id, uuid, state, etc.)
```

### Excepciones — `exception.py`

```python
# Reintenta en N segundos
raise GQRetryableJobError("mensaje", seconds=60)

# Falla definitiva
raise GQFailedJobError("mensaje")

# Completado sin hacer nada
raise GQNothingToDoJob("mensaje")
```

---

## 14. Limitaciones conocidas

| Limitación | Detalle |
|---|---|
| **Sin gestión de canales** | El campo `channel` es informativo. No hay control de capacidad por canal. |
| **Sin detección de jobs huérfanos** | Si Odoo se reinicia con un job en `started`, quedará atascado. Requiere corrección manual (`state → pending`). |
| **Timeout de CronWorker** | Si `limit_time_real_cron` está configurado y un job tarda demasiado, el proceso será interrumpido. Usar `limit_time_real_cron = 0` para deshabilitar. |
| **Recordsets no serializables** | No se pueden pasar recordsets como args/kwargs. Usar IDs. |
| **Sin prioridad por canal** | La selección de jobs es global por `priority` + `date_created`, sin considerar canales. |
