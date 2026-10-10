# Paid Task Scout

**Web dashboard:** `docs/index.html` is a static, responsive viewer that consumes `docs/report.json`; it needs no API keys or backend. To preview locally, run `python3 -m http.server 8000` from `docs/` and open `http://localhost:8000`. Run `python3 scout.py --live` and copy `output/report.json` to `docs/report.json` to refresh the local viewer.

**Autonomous updates:** `.github/workflows/scan.yml` can refresh the publicly available read-only feeds twice per day from a public GitHub repository using standard free GitHub Actions runners. It writes three output files and the static dashboard JSON to the repository. `.github/workflows/ci.yml` runs all offline regression tests on changes; neither workflow sends submissions, uses wallets, creates accounts or incurs paid third-party API charges. The dashboard may be deployed on GitHub Pages from the `/docs` folder, free for a public repository.

Monitor **local, de solo lectura y sin cuentas** para descubrir oportunidades públicas de trabajo entre agentes. Es un producto CLI autónomo, hecho con **Python 3.9+ de la biblioteca estándar**: no instala paquetes, no usa claves, no pide datos privados y no utiliza servicios de pago.

El monitor **no reclama trabajos, presenta propuestas, publica PR, envía mensajes, mueve criptomonedas ni cobra automáticamente**. Leer una recompensa no garantiza recibirla.

## Ejecutar

Desde la carpeta /downloads/paid-task-scout/ (equivale a ~/Downloads/paid-task-scout/ en macOS), ejecuta:

    python3 scout.py --offline
    python3 scout.py --live
    python3 -m unittest discover -s tests -v

El modo offline consume exclusivamente los cinco ficheros JSON sintéticos de fixtures/. Útil para enseñar o probar la herramienta sin Internet. **Su registro «real-funded» es inventado, para probar el filtro: NO representa dinero real.**

El modo live consulta exclusivamente endpoints públicos con métodos **GET** y tiempo de espera de 8 segundos por solicitud, sin autenticación:

| Origen | Endpoint público |
| --- | --- |
| BasedAgents — estado | https://api.basedagents.ai/v1/status |
| BasedAgents — tareas abiertas | https://api.basedagents.ai/v1/tasks?status=open&limit=100 |
| Brick Blue — tareas | https://brick.blue/api/v1/tasks |
| AgentBounties — feed canónico | https://api.agentbounties.app/v1/opportunities?limit=100&network=base-mainnet&source_type=canonical_base&view=ready_to_earn |
| TaskMarket (Daydreams) — tareas abiertas | https://api.taskmarket.dev/api/tasks?status=open&limit=100 |

No se realizan peticiones a las URL que aparezcan dentro de las descripciones de tareas. El máximo leído es de 2 MB por respuesta; se rechazará una página HTML disfrazada de API JSON.

Ambos modos escriben tres ficheros que **se reemplazan en cada escaneo** dentro de output/:

- **output/report.json:** datos normalizados, errores por API, timestamp UTC, evidencias y motivos de clasificación.
- **output/report.md:** reporte legible, categorías, estado de fuentes y restricciones.
- **output/scans.csv:** una fila por anuncio escaneado, con fuente, id, categoría, importe declarado y evidencia. Compatible con Excel en UTF-8 con BOM; se neutralizan fórmulas al exportar.

Se puede usar --output RUTA para elegir otra carpeta local. Solo escribir en una carpeta en la que tengas permiso.

## Qué significan las cinco categorías

1. **real-funded:** BasedAgents presenta importe positivo en USDC, red Base mainnet, escrow declarado funded y deposit_tx_hash válido, sin devolución ni liberación. **Es evidencia declarada por la API**; falta verificación onchain independiente, condiciones de retirada y requisitos de edad. No equivale a dinero recibido ni a contrato válido.
2. **declared-not-funded:** hay premio o una posibilidad sin pruebas suficientes de escrow, o directamente no hay importe real. Por ejemplo, payment_verified=1 sin depósito no basta; paymentState=escrowed en Brick Blue por sí solo tampoco.
3. **sandbox:** moneda o recompensas de pruebas, redes testnet como Base Sepolia o Brick Blue sandbox. No se computan como USDC cobrables en mainnet.
4. **unusable:** tarea marcada como test, spam, demo, benchmark, solicitud ya cerrada/asignada, plazo expirado, exige dinero por adelantado, stake o comprar créditos, pide edad mínima 18+, o TaskMarket muestra más de 3 entregas, operaciones cripto riesgosas, entregas de arte o acceso restringido.
5. **unknown:** origen, pago o integridad de la fuente inciertos. En AgentBounties, un feed degradado no demuestra que no haya oportunidades. En TaskMarket, escrowTxHash es una declaración del servicio que por sí sola **no prueba** un depósito todavía retenido en blockchain.

La clasificación es **conservadora**. Una tarea etiquetada real-funded no significa «apta para menores»: **la elegibilidad legal, país, edad, KYC, condiciones contractuales, posibilidad de cobrar y fiabilidad del sponsor nunca se dan por verificadas**. El escáner elimina tareas que **dicen** exigir 18 años, pero no puede comprobar automáticamente todas las políticas generales de los mercados. Tampoco presupone que una tarea sea realizable en menos de tres horas; el tamaño del trabajo requiere revisión humana.

Los filtros pueden ocasionar falsos negativos y no constituyen auditoría de contratos inteligentes. No se asume que los datos de los marketplaces sean fiables; se registran sus declaraciones y, cuando faltan pruebas, se dice expresamente.

## Diseño y seguridad

- **scout.py** realiza peticiones HTTPS a una lista fija de endpoints públicos y transforma las respuestas a campos comparables.
- **fixtures/** es un juego de datos 100 % inventado que prueba la diferencia entre premio anunciado y depósito marcado como financiado, falsos trabajos TEST con escrow, sandbox, gastos, edad mínima, alta competencia y feeds caídos.
- **tests/test_scout.py** verifica los casos anteriores con unittest, la salida JSON/CSV/Markdown y el comportamiento de una API fallida.
- Si una fuente devuelve error HTTP, HTML, JSON inválido o queda inactiva, aparece ERROR o aviso en el informe. Se sigue con las demás fuentes y el CLI sale con código 2 si falla alguna API. Un fallo no se convierte silenciosamente en «cero oportunidades».
- Las URLs de origen son públicas y fijas; no se envían credenciales, nombres personales, mensajes, direcciones de wallet, datos del equipo ni telemetría.
- No hay registro, servicio en segundo plano, dependencia pip, suscripción, marca personal ni sistema de cobro.

### Posibles ampliaciones

Corroborar cada hash de depósito contra un nodo Base mainnet confiable y contrastar el saldo del contrato, verificaciones de aceptación y restricciones legales por plataforma. Esa integración requeriría otra fuente y análisis del contrato; **no está implementada** y el informe actual no pretende sustituirla.
