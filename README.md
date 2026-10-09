# Arxiv Times

Un lector diario de [arXiv](https://arxiv.org) que corre en tu computadora. Muestra los papers nuevos de tus categorías y te ayuda a encontrar lo que te interesa. Opcionalmente, con [Claude](https://claude.ai), te resume el día y te explica los papers.

Es un solo archivo de Python, sin dependencias: alcanza con el Python 3 que ya trae macOS.

## Qué hace

**Lectura del día**
- Papers anunciados hoy en tus categorías (`hep-th`, `gr-qc`, `quant-ph`, …), con filtros para nuevos, cross-lists y reemplazos. El fin de semana muestra el último anuncio.
- Fórmulas LaTeX bien renderizadas, links al PDF y a arXiv.
- Leídos y no leídos, una lista **Me interesa**, otra **Para leer**, buscador y atajos de teclado.

**Filtrar lo que te interesa**
- Palabras clave resaltadas, y los papers que las mencionan aparecen primero.
- Autores que seguís.
- Papers **parecidos a los que marcaste como "Me interesa"** (TF-IDF), con las palabras que coincidieron.

**Buscador**
- Búsqueda en todo **arXiv** y en **INSPIRE-HEP**, con cantidad de citas y revista.
- Desde cualquier paper: **📈 Citas** (quién lo cita) y **📚 Referencias** (qué cita).

**Asistente de IA** (opcional, usa tu plan de Claude a través de [Claude Code](https://claude.com/claude-code))
- **🧠 Resumen del día:** temas más discutidos, cantidad de papers por área y cuáles te pueden interesar.
- **🧠 Explicar:** lee el paper completo y explica la motivación, los modelos, los cálculos principales, las conclusiones y las preguntas abiertas.
- **💬 Chat:** una conversación sobre el paper completo.

## Instalación (macOS)

```bash
git clone https://github.com/malpartidabr-afk/arxiv-times.git ~/arxiv-times
cd ~/arxiv-times
./install.sh
```

Se abre en `http://localhost:8000` y arranca solo cada vez que iniciás sesión. Para abrirlo después, usá ese link o hacé doble clic en `Abrir Arxiv Times.command`. Si querés otro puerto: `./install.sh 9000`.

Para actualizar a la última versión: `cd ~/arxiv-times && git pull && ./install.sh`.

### Sin instalar (macOS o Linux)

```bash
python3 arxiv_times.py
```

Abre el navegador y queda corriendo hasta que cierres la Terminal (Ctrl+C).

## Asistente de IA

Es opcional: todo lo demás funciona sin esto. Para activarlo:

1. Instalá Claude Code: `curl -fsSL https://claude.ai/install.sh | bash`
2. Corré `~/.local/bin/claude` una vez e iniciá sesión con tu cuenta de Claude (Pro, Max, Team o Enterprise).

Cada resumen, explicación o mensaje de chat descuenta de los límites de uso de tu plan. Como referencia, un resumen del día de una categoría usó menos del 1% del límite de 5 horas de un plan Team. El modelo (Sonnet, Opus o Haiku) se elige en ⚙.

## Atajos de teclado

| Tecla | Acción |
|---|---|
| `j` / `k` | siguiente / anterior |
| `o` o `Enter` | abrir / cerrar abstract |
| `s` | me interesa |
| `l` | para leer |
| `m` | leído / no leído |
| `e` | explicar (IA) |
| `c` | chat (IA) |
| `p` / `a` | abrir PDF / página de arXiv |
| `u` | ocultar / mostrar leídos |
| `/` | buscar |

## Tus datos

Todo queda en tu computadora:

| Qué | Dónde |
|---|---|
| Listas, leídos y preferencias | `~/.arxiv_times.json` |
| Resúmenes, explicaciones, chats y textos de papers | `~/.arxiv_times_ai/` |

Ocupa muy poco: unas decenas de MB por año con uso diario.

## Privacidad y seguridad

- El programa **solo acepta conexiones desde la misma computadora** y rechaza pedidos que no vengan de su propia página.
- Para la lista del día y el buscador se conecta solamente a arXiv e INSPIRE-HEP.
- Si usás el asistente, los abstracts, el texto de los papers y tus preguntas se envían a Anthropic para que Claude los procese, según las condiciones de tu plan.

## Desinstalar

```bash
./uninstall.sh
```

No borra tus datos. Si tampoco los querés: `rm -r ~/.arxiv_times.json ~/.arxiv_times_ai`.

## Licencia

[MIT](LICENSE)
