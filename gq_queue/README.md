# GQ Queue

Módulo Odoo 18 para ejecución asíncrona de tareas en segundo plano mediante CronWorkers de Odoo. No requiere proceso externo ni depende de `queue_job`.

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
│   ├── gq_job_function.py     # Configuración por función (reintentos, on_fail)
│   └── ir_cron.py             # Agrega campo gq_job_runner a ir.cron
├── security/
│   └── ir.model.access.csv
├── tests/
│   └── test_gq_job.py
├── data/
│   └── ir_cron.xml            # Cron "GQ Queue Job Runner"
└── views/
    ├── gq_job_views.xml       # Vistas list, form, search y menú
    ├── gq_job_function_views.xml
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
| `user_id` | Many2one | Usuario que encoló el job. El job se ejecuta con este usuario |
| `company_id` | Many2one | Compañía activa al encolar |
| `run_sudo` | Boolean | El job se encoló desde un recordset con `sudo()` y se ejecuta igual |
| `context` | Text | JSON object con las claves de contexto conservadas (`lang`, `tz`, `allowed_company_ids`, `active_test`) |
| `state` | Selection | Estado actual del job (ver sección 5) |
| `priority` | Integer | Prioridad de ejecución. Menor = más urgente. Default: `10` |
| `eta` | Datetime | No ejecutar antes de esta fecha/hora UTC |
| `max_retries` | Integer | Máximo de reintentos en error retryable. `0` = sin límite. Default: `5` |
| `retry_count` | Integer | Número de intentos iniciados (se cuenta al pasar a `started`) |
| `identity_key` | Char | Clave de identidad: mientras haya un job pendiente con esta clave no se encola otro |
| `group_uuid` | Char | Grupo del job. Si un job del grupo falla, los demás se detienen |
| `depends_on_ids` | Many2many | Jobs que deben estar en `done` antes de ejecutar este |
| `dependent_ids` | Many2many | Jobs que dependen de este |
| `result` | Text | Mensaje de resultado (éxito o descripción del reintento) |
| `exc_name` | Char | Clase de la excepción que causó el fallo, p.ej. `ValueError` |
| `exc_message` | Char | Mensaje de la excepción |
| `exc_info` | Text | Traceback completo en caso de fallo |
| `exec_time` | Float | Segundos que tomó el último intento |
| `worker_pid` | Integer | Proceso que ejecutó el último intento |
| `date_created` | Datetime | Fecha de creación del job |
| `date_enqueued` | Datetime | Última vez que pasó a `pending` (al crearse, al liberarse de `waiting` o al volver a la cola para un reintento) |
| `date_started` | Datetime | Fecha en que el runner comenzó a ejecutarlo |
| `date_done` | Datetime | Fecha de finalización (done, failed o cancelled) |
| `date_cancelled` | Datetime | Fecha de cancelación |

### Orden de procesamiento

Los jobs se toman de la cola ordenados por:
1. `priority ASC` — menor número primero
2. `date_created ASC` — FIFO dentro de la misma prioridad
3. `id ASC` — desempate entre jobs creados en el mismo segundo

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
| `waiting` | En espera: tiene dependencias sin terminar o su grupo está detenido por un job fallido. |
| `pending` | Esperando ser tomado por el runner. Estado inicial. |
| `started` | Runner lo tomó y está ejecutando el método. Se confirma en BD antes de ejecutar. |
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
| `identity_key` | str | `None` | Si ya hay un job **pendiente** con esta clave, no se encola otro y se retorna el existente. |
| `chunk_size` | int | `None` | Divide el recordset en lotes de este tamaño y encola un job por lote. |
| `group` | str | `None` | Identificador de grupo. Los lotes de `chunk_size` forman un grupo solos. |
| `depends_on` | gq.job | `None` | Jobs que deben terminar antes. El job nuevo se une a su grupo. |

`identity_key` y `chunk_size` no se pueden combinar.

### Funciones encolables

Se puede encolar cualquier función del modelo, pública o privada (`_mi_funcion`). Conviene encolar funciones privadas: las públicas son invocables por RPC. Si la función no existe en el modelo, `with_gq_delay()` lanza `AttributeError` al encolar.

### Trabajos grandes: lotes

Un job debe durar menos que el margen de tiempo del runner (ver sección 10). Para procesar muchos registros, dividir en lotes:

```python
# 5 000 facturas → 50 jobs de 100 registros cada uno
invoices.with_gq_delay(chunk_size=100)._procesar()
```

Cada lote es un job independiente: se reintenta y falla por separado. La llamada retorna el recordset con todos los jobs creados.

### Grupos y dependencias

Un **grupo** reúne varios jobs en un solo proceso. Reglas:

- Si un job del grupo pasa a `failed`, **el grupo se detiene**: sus jobs `pending` pasan a
  `waiting` y no se ejecutan. Los que ya están `started` terminan.
- Reencolar el job fallido (**Requeue**) reanuda el grupo.
- Un grupo detenido tampoco admite jobs nuevos: nacen en `waiting`.

Una **dependencia** fuerza el orden: el job no se ejecuta hasta que todos los jobs de
`depends_on` estén en `done`, sin importar la prioridad.

```python
# Lotes en paralelo: forman un grupo automáticamente
lotes = facturas.with_gq_delay(chunk_size=100)._procesar()

# Job final: corre solo cuando todos los lotes terminaron bien
self.with_gq_delay(depends_on=lotes)._notificar_fin()

# Orden forzado entre pasos
paso1 = self.with_gq_delay()._exportar()
paso2 = self.with_gq_delay(depends_on=paso1)._enviar()
paso3 = self.with_gq_delay(depends_on=paso2)._archivar()

# Grupo explícito para jobs encolados por separado
self.with_gq_delay(group=f"import-{self.id}")._importar_hoja(1)
self.with_gq_delay(group=f"import-{self.id}")._importar_hoja(2)
```

Grupo del job nuevo, por orden: el de `group`; el de sus dependencias (si no tienen, se crea
uno y se les asigna); uno nuevo si `chunk_size` genera varios lotes. Con dependencias de
grupos distintos el job no se une a ninguno.

En el formulario del job, el botón **Group Done** muestra hechos / total y abre los jobs del
grupo. **Cancel Group** cancela todos los jobs del grupo que no se han ejecutado.

Cancelar solo el job fallido reanuda el resto del grupo, salvo los jobs que dependen del
cancelado: esos quedan en `waiting`, porque una dependencia cancelada nunca llega a `done`.

### Grafos declarativos: `gq_delayable`, `gq_chain`, `gq_group`

`gq_delayable()` prepara una llamada **sin encolarla**; acepta las mismas opciones que
`with_gq_delay()`. Las llamadas se combinan y todo el grafo se encola con `delay()`:

```python
from odoo.addons.gq_queue.delay import gq_chain, gq_group

# Secuencia: cada paso espera al anterior
gq_chain(
    self.gq_delayable()._exportar(),
    self.gq_delayable()._enviar(),
    self.gq_delayable()._archivar(),
).delay()

# Paralelo y, al terminar todos, un paso final
gq_group(
    lote1.gq_delayable()._procesar(),
    lote2.gq_delayable()._procesar(),
).on_done(self.gq_delayable()._notificar_fin()).delay()

# Combinado: inicio -> (b y c en paralelo) -> fin
gq_chain(
    self.gq_delayable()._inicio(),
    gq_group(a.gq_delayable()._paso(), b.gq_delayable()._paso()),
    self.gq_delayable(priority=1)._fin(),
).delay()
```

| Elemento | Efecto |
|---|---|
| `gq_chain(a, b, c)` | Ejecuta en orden: cada nodo depende del anterior. |
| `gq_group(a, b, c)` | Sin orden entre sí: pueden correr en paralelo. |
| `nodo.on_done(x, y)` | `x` e `y` corren cuando `nodo` terminó bien. Retorna `nodo`. |
| `llamada.set(priority=1)` | Cambia opciones de una llamada ya preparada. |
| `nodo.delay()` | Encola **todo** el grafo conectado y retorna sus `gq.job`. |

Los jobs de un grafo comparten grupo: si uno falla, el resto no se ejecuta hasta
reencolarlo. Una dependencia circular lanza `ValueError` en `delay()`. Por debajo es el
mismo mecanismo de `depends_on` y `group`.

### Ejecutar en línea, sin encolar

Con `gq_queue__no_delay` en el contexto, o la variable de entorno `GQ_QUEUE__NO_DELAY=1`,
las funciones se ejecutan de inmediato en lugar de encolarse. Pensado para tests y
depuración de módulos que encolan.

```python
# En un test: el efecto es inmediato, sin correr el runner
record.with_context(gq_queue__no_delay=True).accion_que_encola()
```

En este modo `with_gq_delay().funcion()` retorna el resultado de la función (lista de
resultados con `chunk_size`) en lugar del `gq.job`, y `delay()` de un grafo ejecuta las
funciones en orden y retorna `None`.

### Encolar automáticamente una función existente

`_patch_gq_job_auto_delay` vuelve asíncrona una función sin modificar a quien la llama.
Útil cuando la llamada está en código de otro módulo.

```python
def _generar_pdf(self, plantilla):
    ...

# Opcional: opciones del job, con los mismos argumentos de la función
def _generar_pdf_gq_job_options(self, plantilla):
    return {"priority": 20, "description": f"PDF {plantilla}"}

def _register_hook(self):
    self._gq_patch_method(
        "_generar_pdf",
        self._patch_gq_job_auto_delay("_generar_pdf", context_key="gq_auto_delay_pdf"),
    )
    return super()._register_hook()

# Solo se encola cuando quien llama lo pide por contexto:
self.with_context(gq_auto_delay_pdf=True).accion_que_genera_pdf()
```

Usar siempre `context_key`: sin él la función se encola en **todas** las llamadas, y eso
rompe a quien espera su resultado o su efecto inmediato. Dentro de un job y en modo sin
encolado la función se ejecuta directamente.

### Evitar duplicados: clave de identidad

```python
# Varios clics seguidos encolan un solo job
self.with_gq_delay(identity_key=f"sync-partner-{self.id}")._sincronizar()
```

Solo cuentan los jobs en `pending` o `waiting`. Un job ya iniciado no bloquea la clave, porque puede haber leído datos anteriores al nuevo encolado.

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

| recordset | `{"__gqtype__": "recordset", "model": "res.partner", "ids": [1, 2], "su": false}` |

### Recordsets como argumentos

```python
record.with_gq_delay()._mi_funcion(otros_registros, cliente=partner)
```

Se guardan modelo e ids. Al ejecutar se reconstruyen en el entorno del job (usuario y
contexto de quien encoló), conservando el modo `sudo()` del argumento. Un registro borrado
antes de ejecutar da error al leerlo: usar `.exists()` si eso es posible.

Cualquier otro tipo no serializable lanza `TypeError` al encolar.

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

Espera antes del reintento, por orden: `seconds` del error; el patrón de reintentos
configurado para la función (ver sección 9.1); 5 segundos.

```python
# No consume intentos: el job puede reintentar sin límite.
# Usar solo cuando la condición de espera termina con seguridad.
raise GQRetryableJobError("Esperando cierre de caja", seconds=60, ignore_retry=True)
```

**Comportamiento:**
- `retry_count` cuenta intentos iniciados; se incrementa al pasar a `started`.
- Un job admite `max_retries` reintentos después del primer intento: si `retry_count > max_retries` (y `max_retries > 0`), pasa a `failed`.
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

El job guarda la clase y el mensaje de la excepción en `exc_name` y `exc_message`, para
filtrar y agrupar fallos en la lista.

### 9.1 Configuración por función

En **GQ Queue → Job Functions** se configura, por modelo y función:

| Campo | Efecto |
|---|---|
| `retry_pattern` | Espera según el número de intento, ante errores reintentables sin `seconds`. |
| `on_fail_method` | Función del modelo que se llama cuando el job falla definitivamente. |

Sin registro para una función, sus jobs usan los valores por defecto.

**Patrón de reintentos.** Diccionario `{intento: segundos}`. Aplica la entrada más alta que
no supere el intento actual; antes de la primera entrada, 5 segundos. Un par `(min, max)`
elige la espera al azar en ese rango, para no reintentar todos los jobs a la vez.

```python
{1: 10, 5: 60, 10: 300}        # intentos 1-4: 10 s; 5-9: 60 s; 10+: 300 s
{1: (1, 10), 5: (30, 60)}      # espera aleatoria por tramo
```

**Función `on_fail`.** Se llama sobre el recordset del job cuando pasa a `failed` (error no
reintentable, reintentos agotados o worker muerto), no en cada reintento:

```python
def _al_fallar_sync(self, exc_name, exc_message, exc_info):
    self.write({"sync_state": "error", "sync_error": exc_message})
```

Un error dentro de `on_fail` se registra en el log y no se propaga: el runner sigue con el
siguiente job.

También por datos XML, desde el módulo que define la función:

```xml
<record id="job_function_sync" model="gq.job.function">
    <field name="model_id" ref="model_mi_modelo"/>
    <field name="method">_sincronizar</field>
    <field name="retry_pattern">{1: 10, 5: 60, 10: 300}</field>
    <field name="on_fail_method">_al_fallar_sync</field>
</record>
```

### 9.2 Saber en qué job corre la función

Durante la ejecución, el contexto incluye `job_uuid`:

```python
def _procesar(self):
    job = self.env["gq.job"].sudo().search([("uuid", "=", self.env.context["job_uuid"])])
```

### Errores de concurrencia PostgreSQL

Los errores de serialización de PostgreSQL (`OperationalError` con códigos en `PG_CONCURRENCY_ERRORS_TO_RETRY`) son capturados automáticamente. El job vuelve a `pending` con una ETA de 5 segundos sin consumir un reintento.

---

## 10. Runner y sistema de triggers

### Cómo se activa el runner

El cron se despierta a demanda. Su intervalo propio (10 minutos) es solo un respaldo
por si se pierde un disparo; una corrida sin jobs cuesta dos consultas.

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
def _gq_job_runner(self, commit=True, max_seconds=None):
    self._gq_recover_started_jobs()    # reencola o falla jobs huérfanos
    self._gq_release_waiting()         # libera jobs con dependencias ya cumplidas
    job = self._gq_acquire_one_job()   # SQL SELECT ... FOR NO KEY UPDATE SKIP LOCKED
    while job:
        job._gq_process(commit=commit) # ejecuta + actualiza estado
        if presupuesto_agotado:
            self._gq_cron_trigger()    # continúa en la siguiente ejecución del cron
            break
        job = self._gq_acquire_one_job()
    # termina cuando no hay más jobs pendientes o se agota el presupuesto
```

### Presupuesto de tiempo

`max_seconds` es por defecto **la mitad del límite de tiempo del servidor** para crons
(`limit_time_real_cron`; si vale `-1`, `limit_time_real`). La otra mitad queda como margen
para que termine el job en curso. Sin límite configurado (`0`), el presupuesto es 600 s.

El presupuesto se revisa **entre jobs**: un job individual debe durar menos que el margen
(la mitad del límite). Un job más largo debe dividirse en varios jobs.

### Jobs huérfanos

Mientras un job corre, el runner mantiene un **bloqueo consultivo de PostgreSQL**
(`pg_try_advisory_xact_lock`) en una conexión aparte, y confirma en BD el estado `started`
y el intento **antes** de ejecutar. Si el worker muere (timeout, memoria, reinicio),
PostgreSQL suelta el bloqueo al cerrarse la conexión y el job queda en `started` sin él: es
un huérfano. El siguiente runner lo detecta de inmediato, sin plazos, y:

- lo devuelve a `pending` con 60 segundos de espera, si le quedan intentos;
- lo pasa a `failed` si `retry_count > max_retries` (con `max_retries = 0`, tras 5 intentos).

Así un job que mata al worker no se reintenta sin fin, ni varias veces seguidas.

La detección no depende de `limit_time_real_cron` ni del modo del servidor: funciona igual
en Odoo.sh, en servidores propios con o sin límite de tiempo y en modo de hilos.

### Funciones que hacen `cr.commit()`

Se permite sin configuración. La función corre en el cursor del runner, que es propio del
cron y está aislado de las peticiones de usuarios:

- Lo que la función confirmó persiste aunque después falle; lo no confirmado se revierte.
- El bloqueo de vida está en otra conexión, así que el commit no lo suelta: el job no se
  confunde con un huérfano ni se ejecuta dos veces.

Útil para procesos largos que guardan avance, o que llaman a funciones estándar que
confirman por su cuenta. La función `on_fail` es la excepción: no debe hacer commit.

### Flujo interno de `_gq_process()`

```
_gq_alive_lock()           → bloqueo consultivo en otra conexión: el job está vivo
_gq_set_started()          → state = 'started', date_started = now(), retry_count + 1
commit                     → 'started' visible; si el worker muere, el job es huérfano
    │
    ├── _gq_guard()                        → ante un error, rollback de lo no confirmado
    │       ├── _gq_perform()              → ejecuta model.method(*args, **kwargs)
    │       │                                con usuario, sudo y contexto del encolado
    │       └── _gq_set_done()            → state = 'done'
    │
    ├── OperationalError (PG concurrency) → _gq_postpone_pending(seconds=5, reset_retry=True)
    ├── GQNothingToDoJob                  → _gq_set_done(result=msg)
    ├── GQRetryableJobError               → _gq_postpone_pending() o _gq_set_failed()
    └── GQFailedJobError / Exception      → _gq_set_failed(exc_info=traceback)
commit                     → resultado visible; se suelta el bloqueo consultivo
```

---

## 11. Concurrencia y paralelismo

### Bloqueo PostgreSQL

El método `_gq_acquire_one_job()` usa:

```sql
SELECT id FROM gq_job
WHERE state = 'pending'
  AND (eta IS NULL OR eta <= (now() AT TIME ZONE 'UTC'))
ORDER BY priority ASC, date_created ASC, id ASC
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

# Límite de tiempo real de una ejecución de cron. El runner usa la mitad como
# presupuesto. En Odoo.sh no es configurable: el runner lee el valor vigente.
# limit_time_real_cron = 900
```

### Limpieza automática

El autovacuum de Odoo borra los jobs `done` y `cancelled` con más de 30 días.
Los `failed` se conservan para su revisión. Días configurables con el parámetro del
sistema `gq_queue.keep_done_days` (`0` = no borrar).

### Gestión desde la interfaz

En **GQ Queue → Jobs** (solo `base.group_system`), desde el formulario o seleccionando
varios en la lista:

- **Requeue**: vuelve a encolar un job `failed`, `cancelled` o `done` y reinicia sus intentos. Pasa a `pending`, o queda en `waiting` si tiene dependencias sin terminar o su grupo sigue detenido.
- **Cancel**: cancela un job `waiting`, `pending` o `failed`. Un job `started` no se puede cancelar.
- **Cancel Group**: cancela todos los jobs no ejecutados del grupo.

### Requisitos del servidor

Ninguno fuera de Odoo estándar: no hay proceso externo, ni `server_wide_modules`, ni
peticiones HTTP a sí mismo. Solo deben estar activos los crons (`max_cron_threads > 0`).

| Entorno | Funciona | Notas |
|---|---|---|
| Odoo.sh | Sí | El presupuesto se ajusta al límite de la plataforma. |
| Servidor propio, multiproceso (`workers > 0`) | Sí | Con o sin `limit_time_real_cron`. |
| Servidor propio, hilos (`workers = 0`) | Sí (probado) | Servidor real con 3 hilos de cron y 3 crons runner. |
| Varios runners en paralelo | Sí (probado) | Procesos reales en paralelo: ningún job repetido, cadenas en orden. |
| Varias instancias de Odoo sobre la misma BD | Sí (probado) | Procesos independientes sobre la misma BD; el bloqueo y la cola viven en PostgreSQL. |
| Muerte del worker durante un job | Sí (probado) | Proceso matado a mitad del job: se recupera y se reintenta; tras agotar intentos queda `failed` (`GQDeadJobError`). |

Cada runner en ejecución usa una conexión adicional a PostgreSQL (la del bloqueo de vida),
solo mientras procesa un job.

#### Pruebas realizadas

- **Servidor Odoo real** (`workers = 0`, `max_cron_threads = 3`, 3 crons runner): 2505 jobs
  (incluida una cadena de 5) terminados en 48 s, sin ejecuciones repetidas, cadena en orden,
  los 3 crons procesaron jobs, `failure_count = 0` y sin errores en el log.
- **Procesos en paralelo**: 3 runners más 1 que se suma tarde, 159 ejecuciones sin repetidos;
  el grupo se detiene tras el fallo de un job.
- **Muerte real del worker** (proceso terminado con `taskkill`): el job se recuperó como
  huérfano, se reintentó a los 60 s y los datos confirmados antes de morir se conservaron; al
  repetirse la muerte quedó `failed` y el resto de la cola siguió.

#### Concurrencia entre runners

Odoo usa aislamiento `REPEATABLE READ`: cuando dos runners tocan las mismas filas de la cola,
PostgreSQL rechaza a uno con un error de serialización. No es un fallo: el runner revierte y
reintenta (hasta 10 veces); si el conflicto persiste, deja el trabajo para otra pasada del cron.
Estos conflictos no se registran como errores ni cuentan como fallo del cron.

Encolar muchos jobs en una transacción crea un solo disparo inmediato por cron: cada disparo
abre una conexión para avisar a los workers de cron al confirmar.

### Permisos

- Encolar con `with_gq_delay()` no requiere permisos sobre `gq.job` (se crea con `sudo()`).
- Solo `base.group_system` puede leer y gestionar los jobs y ver el menú **GQ Queue**.
- El job se ejecuta con el usuario, el modo `sudo()` y el contexto de quien lo encoló.

### Variables de entorno

No se requieren variables de entorno adicionales.

---

## 13. Referencia de API

### `gq.job` — métodos públicos relevantes

| Método | Descripción |
|---|---|
| `action_requeue()` | Vuelve a encolar jobs terminados y reinicia sus intentos |
| `action_cancel()` | Cancela jobs en espera, pendientes o fallidos |
| `action_cancel_group()` | Cancela todos los jobs no ejecutados del grupo |
| `_gq_release_waiting()` | Pasa a `pending` los jobs en `waiting` que ya pueden ejecutarse |
| `_gq_job_runner(commit=True, max_seconds=None)` | Entry point del cron. Procesa la cola dentro del presupuesto de tiempo. |
| `_gq_cron_trigger(at=None)` | Dispara todos los crons `gq_job_runner`. |

### `GQDelayable` — `delay.py`

Clase proxy usada internamente por `with_gq_delay()`. También puede instanciarse directamente para uso avanzado:

```python
from odoo.addons.gq_queue.delay import GQDelayable

GQDelayable(recordset, priority=10, eta=None, description=None, max_retries=5,
            identity_key=None, chunk_size=None, group=None, depends_on=None)

# Llamada diferida y grafos
GQDeferred(recordset, **opciones)      # lo que retorna gq_delayable()
gq_chain(*nodos)  /  gq_group(*nodos)  # nodos: GQDeferred, cadenas o grupos
```

Al llamar cualquier función sobre el proxy, crea y retorna el `gq.job` (varios con `chunk_size`):

```python
job = self.env["sale.order"].browse([1]).with_gq_delay().action_confirm()
# job es el registro gq.job creado (id, uuid, state, etc.)
```

### Excepciones — `exception.py`

```python
# Reintenta en N segundos
raise GQRetryableJobError("mensaje", seconds=60)

# Reintenta sin consumir un intento
raise GQRetryableJobError("mensaje", ignore_retry=True)

# Falla definitiva
raise GQFailedJobError("mensaje")

# Completado sin hacer nada
raise GQNothingToDoJob("mensaje")
```

---

## 14. Limitaciones conocidas

| Limitación | Detalle |
|---|---|
| **Sin canales** | No hay canales ni control de capacidad como en `queue_job`. El paralelismo lo da el número de crons runner. |
| **Job más largo que el margen** | Un job que dura más que la mitad del límite de tiempo del servidor puede ser interrumpido. Se reintenta como huérfano hasta agotar intentos. Dividirlo en jobs más chicos. |
