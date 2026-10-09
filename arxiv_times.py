#!/usr/bin/env python3
"""Arxiv Times — lector de los papers nuevos de arXiv.

Uso:
    python3 arxiv_times.py              # abre http://localhost:8000
    python3 arxiv_times.py --port 9000
    python3 arxiv_times.py --no-browser

Sin dependencias: solo la biblioteca estándar de Python 3.
Tus datos (leídos, "Me interesa", "Para leer", preferencias) se guardan en ~/.arxiv_times.json
Solo acepta conexiones desde esta misma computadora.
"""
import argparse
import html as htmllib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}
UA = "arxiv-times/1.0 (lector personal)"
CACHE_TTL = 30 * 60  # segundos
STATE_FILE = os.environ.get("ARXIV_TIMES_STATE", os.path.expanduser("~/.arxiv_times.json"))
DEFAULT_STATE = {
    "cats": ["hep-th", "gr-qc", "quant-ph"],
    "keywords": [],
    "authors": [],
    "read": [],
    "favs": {},    # "Me interesa": alimenta el criterio de parecidos
    "toread": {},  # "Para leer"
    "aiModel": "sonnet",
}
MAX_READ = 20000

_cache = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- descarga

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for intento in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as ex:
            if ex.code not in (429, 503) or intento == 2:
                raise
            time.sleep(5 * (intento + 1))  # arXiv limita la frecuencia de consultas


def clean(s):
    return re.sub(r"\s+", " ", htmllib.unescape(s or "")).strip()


def strip_tags(s):
    return clean(re.sub(r"<[^>]+>", " ", s or ""))


def parse_rss(xml_bytes):
    """Feed diario oficial (rss.arxiv.org): trae lo anunciado hoy."""
    root = ET.fromstring(xml_bytes)
    date = (root.findtext("a:updated", "", NS) or "")[:10]
    out = []
    for e in root.findall("a:entry", NS):
        summary = e.findtext("a:summary", "", NS)
        m = re.search(r"Abstract:\s*(.*)", summary, re.S)
        link = e.find("a:link", NS)
        abs_url = link.get("href") if link is not None else ""
        aid = abs_url.split("/abs/")[-1]
        authors = e.findtext("dc:creator", "", NS)
        out.append({
            "id": aid,
            "title": clean(e.findtext("a:title", "", NS)),
            "authors": [clean(a) for a in re.split(r",\s*|\s+and\s+", authors) if a.strip()],
            "abstract": clean(m.group(1) if m else summary),
            "categories": [c.get("term") for c in e.findall("a:category", NS)],
            "type": e.findtext("arxiv:announce_type", "new", NS),
            "date": date,
        })
    return out


def parse_listing(page):
    """Página https://arxiv.org/list/<cat>/new: último anuncio (sirve también el fin de semana)."""
    m = re.search(r"Showing new listings for \w+, (\d+ \w+ \d{4})", page)
    date = datetime.strptime(m.group(1), "%d %B %Y").strftime("%Y-%m-%d") if m else ""
    kinds = {"New": "new", "Cross": "cross", "Replacement": "replace"}
    out = []
    for sec in re.finditer(r"<h3>(New|Cross|Replacement) submissions.*?</h3>(.*?)(?=<h3>|\Z)", page, re.S):
        for dt, dd in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", sec.group(2), re.S):
            idm = re.search(r'href ?="/abs/([^"]+)"', dt)
            if not idm:
                continue
            title = re.search(r"<div class=.list-title[^>]*>(.*?)</div>", dd, re.S)
            authors = re.search(r"<div class=.list-authors.>(.*?)</div>", dd, re.S)
            subjects = re.search(r"<div class=.list-subjects.>(.*?)</div>", dd, re.S)
            abstract = re.search(r"<p class=.mathjax.>(.*?)</p>", dd, re.S)
            out.append({
                "id": idm.group(1),
                "title": strip_tags(title.group(1)).removeprefix("Title:").strip() if title else "",
                "authors": [strip_tags(a) for a in re.findall(r"<a[^>]*>(.*?)</a>", authors.group(1))] if authors else [],
                "abstract": strip_tags(abstract.group(1)) if abstract else "",
                "categories": re.findall(r"\(([a-zA-Z\-]+(?:\.[a-zA-Z\-]+)?)\)", subjects.group(1)) if subjects else [],
                "type": kinds[sec.group(1)],
                "date": date,
            })
    return out


def load_papers(cats):
    papers = parse_rss(fetch("https://rss.arxiv.org/atom/" + "+".join(cats)))
    if papers:
        return papers
    # Sin anuncio hoy (sábado, domingo, feriado): uso las páginas de listado.
    rank = {"new": 0, "cross": 1, "replace": 2}
    seen = {}
    for i, cat in enumerate(cats):
        if i:
            time.sleep(1)  # ser amable con arXiv
        page = fetch(f"https://arxiv.org/list/{cat}/new?skip=0&show=2000").decode("utf-8", "replace")
        for p in parse_listing(page):
            old = seen.get(p["id"])
            if old is None or rank[p["type"]] < rank[old["type"]]:
                seen[p["id"]] = p
    return sorted(seen.values(), key=lambda p: rank[p["type"]])


def get_papers(cats, refresh=False):
    key = tuple(sorted(set(cats)))
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    papers = load_papers(list(key))
    for p in papers:
        p["abs"] = f"https://arxiv.org/abs/{p['id']}"
        p["pdf"] = f"https://arxiv.org/pdf/{p['id']}"
    val = {"fetched": time.strftime("%Y-%m-%d %H:%M"), "papers": papers}
    with _lock:
        _cache[key] = (time.time(), val)
    return val


# ---------------------------------------------------------------- estado

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return {**DEFAULT_STATE, **json.load(f)}
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_STATE))


def save_state(update):
    with _lock:
        state = load_state()
        for k in DEFAULT_STATE:
            if k in update:
                state[k] = update[k]
        state["read"] = state["read"][-MAX_READ:]
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
        return state


# ---------------------------------------------------------------- asistente de IA
# Usa Claude Code (`claude -p`), así cada pedido descuenta de tu plan de Claude.

AI_DIR = os.environ.get("ARXIV_TIMES_AI_DIR", os.path.expanduser("~/.arxiv_times_ai"))
AI_MODELS = {"sonnet", "opus", "haiku"}
MAX_PAPER_CHARS = 300_000  # ~75k tokens; lo que pase de esto se corta
_job_locks = {}

NO_CLAUDE_MSG = ("No encontré Claude Code en esta Mac. Instalalo desde la Terminal con "
                 "«curl -fsSL https://claude.ai/install.sh | bash», corré «claude» una vez para "
                 "iniciar sesión con tu cuenta y volvé a intentar.")
NOT_LOGGED_MSG = ("Claude Code no tiene una sesión iniciada. Abrí la Terminal, corré «claude» e "
                  "iniciá sesión con tu cuenta de Claude; después volvé a intentar.")

AREAS = [
    "Relatividad general y gravedad clásica", "Agujeros negros", "Ondas gravitacionales",
    "Cosmología", "Gravedad modificada", "Gravedad cuántica", "Teoría de cuerdas",
    "Holografía y AdS/CFT", "Teoría cuántica de campos", "Física de partículas y fenomenología",
    "Entrelazamiento e información cuántica", "Computación cuántica",
    "Óptica y tecnologías cuánticas", "Fundamentos de la mecánica cuántica",
    "Sistemas de muchos cuerpos y materia condensada", "Física matemática", "Otros",
]

DIGEST_SYSTEM = """Sos un asistente para un investigador de física teórica que lee arXiv todos los días. Vas a recibir el perfil de intereses del lector y la lista completa de papers anunciados hoy (nuevos y cross-lists) en sus categorías, cada uno con su identificador entre corchetes, su tipo, sus categorías, el título y el abstract.

Escribí en castellano, claro y conciso, en Markdown, con fórmulas en LaTeX entre $...$. Cada vez que menciones un paper citá su identificador entre corchetes, por ejemplo [2610.00478], sin agregar links.

Usá exactamente esta estructura:

## Panorama del día
Dos o tres oraciones sobre qué dominó el día.

## Temas más discutidos
Entre 5 y 8 temas concretos, más específicos que un área (por ejemplo "modos cuasinormales" o "entropía de entrelazamiento en QFT"), ordenados de más a menos papers. Para cada uno: el nombre en negrita, cuántos papers lo tratan, una oración sobre qué se está haciendo y los identificadores de esos papers.

## Te pueden interesar
Entre 5 y 10 papers elegidos según el perfil del lector (palabras clave, autores que sigue y papers que marcó como "Me interesa"). Para cada uno: identificador, título abreviado y una oración sobre por qué le puede interesar. Si el perfil está vacío, elegí los papers más novedosos del día y aclaralo.

Después del Markdown escribí una línea que diga exactamente ===CLASIFICACION=== y a continuación una línea por cada paper de la lista, sin excepción, con el formato:
identificador | área
Asigná a cada paper una sola área principal, usando preferentemente estos nombres exactos: """ + "; ".join(AREAS) + """. Usá otro nombre solo si ninguno encaja, y "Otros" como última opción. No escribas nada después de la clasificación."""

EXPLAIN_SYSTEM = """Sos un físico teórico que le explica un paper a un colega investigador. Vas a recibir el paper (texto completo, o un PDF que tenés que leer con la herramienta Read). Escribí en castellano una explicación de entre 400 y 700 palabras, en Markdown, con fórmulas en LaTeX entre $...$ ($$...$$ para ecuaciones destacadas). Usá exactamente estas secciones:

### Motivación
Qué problema abordan y por qué importa.
### Modelos y sistemas estudiados
Qué teoría, modelo, geometría o sistema estudian y bajo qué supuestos.
### Cálculos principales
Las técnicas y pasos centrales, con las ecuaciones clave cuando ayuden.
### Conclusiones
Qué encuentran y qué tan generales son los resultados.
### Ideas y preguntas abiertas
Lo que los autores proponen como trabajo futuro, y otras preguntas o extensiones que te parezcan interesantes (aclarando cuáles son tuyas).

Sé preciso: no inventes resultados que no estén en el texto, y si algo no queda claro en el paper, decilo. Respondé solo con la explicación."""

CHAT_SYSTEM = """Sos un físico teórico que conversa con un colega investigador sobre un paper de arXiv. Al comienzo de la conversación recibís el paper (texto completo, o un PDF que tenés que leer con la herramienta Read). Respondé en castellano, de forma directa y técnica, en Markdown y con fórmulas en LaTeX entre $...$ ($$...$$ para ecuaciones destacadas). Basate en el paper: si algo no está en el texto, decilo, y distinguí claramente tu opinión o conocimiento general de lo que dice el paper. Cuando te refieras a una parte del paper, indicá la sección o la ecuación."""


class UserError(Exception):
    """Error con un mensaje para mostrarle al usuario."""


class AIError(UserError):
    pass


def job_lock(key):
    with _lock:
        return _job_locks.setdefault(key, threading.Lock())


def safe_name(pid):
    return re.sub(r"[^\w.\-]", "_", pid)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def find_claude():
    for c in (os.environ.get("ARXIV_TIMES_CLAUDE"), shutil.which("claude"), "~/.local/bin/claude",
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude", "~/.claude/local/claude"):
        if c:
            c = os.path.expanduser(c)
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
    return None


def run_claude(prompt, system, model, stdin_text="", resume=None, persist=False, tools=None):
    """Corre `claude -p` y devuelve (respuesta, session_id)."""
    exe = find_claude()
    if not exe:
        raise AIError(NO_CLAUDE_MSG)
    model = model if model in AI_MODELS else "sonnet"
    if prompt.startswith("-"):
        prompt = " " + prompt  # que no se confunda con una opción
    cmd = [exe, "-p", prompt, "--output-format", "json", "--model", model,
           "--system-prompt", system, "--strict-mcp-config", "--tools", ",".join(tools or [])]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    if model != "haiku":
        cmd += ["--effort", "medium"]
    if resume:
        cmd += ["--resume", resume]
    if not persist:
        cmd.append("--no-session-persistence")
    os.makedirs(AI_DIR, exist_ok=True)
    try:
        r = subprocess.run(cmd, input=stdin_text, capture_output=True, text=True, timeout=900, cwd=AI_DIR)
    except subprocess.TimeoutExpired:
        raise AIError("Claude tardó más de 15 minutos en responder.")
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise AIError("Error al correr Claude Code: " + (r.stderr or r.stdout or "sin salida").strip()[:400])
    if out.get("is_error"):
        msg = str(out.get("result") or out.get("subtype") or "error desconocido")
        raise AIError(NOT_LOGGED_MSG if "login" in msg.lower() else msg)
    return out.get("result", ""), out.get("session_id")


def html_to_text(page):
    """Texto de la versión HTML de arXiv, con las fórmulas como LaTeX."""
    m = re.search(r"<article\b.*?</article>", page, re.S)
    body = m.group(0) if m else page
    body = re.sub(r"<(script|style|nav|header|footer)\b.*?</\1>", " ", body, flags=re.S)
    body = re.sub(r"<section[^>]*ltx_bibliography.*?</section>", " ", body, flags=re.S)

    def math(mm):
        alt = re.search(r'alttext="([^"]*)"', mm.group(1))
        if not alt:
            return " "
        tex = htmllib.unescape(alt.group(1))
        return f" $${tex}$$ " if 'display="block"' in mm.group(1) else f" ${tex}$ "

    body = re.sub(r"<math\b([^>]*)>.*?</math>", math, body, flags=re.S)
    body = re.sub(r"<h(\d)[^>]*>", lambda h: "\n\n" + "#" * int(h.group(1)) + " ", body)
    body = re.sub(r"</(p|div|h\d|li|tr|figcaption|section|table)>", "\n", body)
    text = htmllib.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def paper_context(p):
    """Contenido completo de un paper para pasarle a Claude: (texto, herramientas)."""
    pid = p["id"]
    head = (f"TÍTULO: {p.get('title', '')}\nAUTORES: {', '.join(p.get('authors', []))}\n"
            f"arXiv: {pid}\n\nABSTRACT: {p.get('abstract', '')}\n")
    d = os.path.join(AI_DIR, "papers")
    os.makedirs(d, exist_ok=True)
    txt = os.path.join(d, safe_name(pid) + ".txt")
    if not os.path.exists(txt):
        try:
            text = html_to_text(fetch(f"https://arxiv.org/html/{pid}").decode("utf-8", "replace"))
            if len(text) > 2000:
                with open(txt, "w", encoding="utf-8") as f:
                    f.write(text)
        except urllib.error.HTTPError:
            pass  # sin versión HTML: uso el PDF
    if os.path.exists(txt):
        with open(txt, encoding="utf-8") as f:
            text = f.read()
        if len(text) > MAX_PAPER_CHARS:
            text = text[:MAX_PAPER_CHARS] + "\n\n[... texto cortado por longitud ...]"
        return head + "\nTEXTO COMPLETO DEL PAPER:\n\n" + text, None
    pdf = os.path.join("papers", safe_name(pid) + ".pdf")
    if not os.path.exists(os.path.join(AI_DIR, pdf)):
        with open(os.path.join(AI_DIR, pdf), "wb") as f:
            f.write(fetch(f"https://arxiv.org/pdf/{pid}"))
    return head + f"\nEl texto completo está en el PDF {pdf} (relativo al directorio actual). Leelo con la herramienta Read.\n", ["Read"]


def digest_path(cats):
    data = get_papers(cats)
    papers = [p for p in data["papers"] if p["type"] in ("new", "cross")]
    date = papers[0]["date"] if papers else time.strftime("%Y-%m-%d")
    key = f"{date}_{'+'.join(sorted(set(cats)))}"
    return os.path.join(AI_DIR, "digest", safe_name(key) + ".json"), papers, date


def make_digest(cats, model, refresh=False):
    path, papers, date = digest_path(cats)
    if not papers:
        raise AIError("No hay papers nuevos para resumir.")
    with job_lock(path):
        cached = read_json(path)
        if cached and not refresh:
            return cached
        st = load_state()
        likes = [f"- {p['title']}" for p in list(st["favs"].values())[-40:]]
        profile = (f"Palabras clave: {', '.join(st['keywords']) or '(ninguna)'}\n"
                   f"Autores que sigue: {', '.join(st['authors']) or '(ninguno)'}\n"
                   f"Papers que marcó como \"Me interesa\":\n" + ("\n".join(likes) or "(ninguno)"))
        listing = "\n\n".join(f"[{p['id']}] ({p['type']}; {', '.join(p['categories'])})\n{p['title']}\n{p['abstract']}"
                              for p in papers)
        result, _ = run_claude("Prepará el resumen del día siguiendo las instrucciones.", DIGEST_SYSTEM, model,
                               f"PERFIL DEL LECTOR:\n{profile}\n\nPAPERS DEL DÍA ({len(papers)}):\n\n{listing}")
        md, _, cls = result.partition("===CLASIFICACION===")
        ids = {p["id"] for p in papers}
        areas = {}
        for line in cls.splitlines():
            m = re.match(r"\s*[-*]?\s*\[?([\w./\-]+?)(?:v\d+)?\]?\s*\|\s*(.+?)\s*$", line)
            if m and m.group(1) in ids:
                areas[m.group(1)] = m.group(2)
        for pid in ids - areas.keys():
            areas[pid] = "Sin clasificar"
        out = {"date": date, "cats": cats, "n": len(papers), "model": model,
               "generated": time.strftime("%Y-%m-%d %H:%M"), "markdown": md.strip(), "areas": areas}
        write_json(path, out)
        return out


def explain(p, model, refresh=False):
    path = os.path.join(AI_DIR, "explain", safe_name(p["id"]) + ".json")
    with job_lock(path):
        cached = read_json(path)
        if cached and not refresh:
            return cached
        ctx, tools = paper_context(p)
        result, _ = run_claude("Explicá este paper siguiendo las instrucciones.", EXPLAIN_SYSTEM, model, ctx, tools=tools)
        out = {"id": p["id"], "model": model, "generated": time.strftime("%Y-%m-%d %H:%M"), "markdown": result.strip()}
        write_json(path, out)
        return out


def chat_path(pid):
    return os.path.join(AI_DIR, "chats", safe_name(pid) + ".json")


def chat(p, message, model):
    path = chat_path(p["id"])
    with job_lock(path):
        st = read_json(path, {"session": None, "messages": []})
        ctx, tools = paper_context(p)
        reply = sid = None
        if st["session"]:
            try:
                reply, sid = run_claude(message, CHAT_SYSTEM, model, resume=st["session"], persist=True, tools=tools)
            except AIError as ex:
                if "conversation" not in str(ex).lower():
                    raise
                st["session"] = None  # la sesión de Claude Code ya no existe: empiezo otra
        if reply is None:
            prev = "\n\n".join(f"{'YO' if m['role'] == 'user' else 'VOS'}: {m['text']}" for m in st["messages"])
            stdin = ctx + (f"\n\nCONVERSACIÓN ANTERIOR SOBRE ESTE PAPER:\n{prev}" if prev else "")
            reply, sid = run_claude(message, CHAT_SYSTEM, model, stdin, persist=True, tools=tools)
        st["session"] = sid
        st["messages"] += [{"role": "user", "text": message}, {"role": "assistant", "text": reply.strip()}]
        write_json(path, st)
        return st


def valid_paper(p):
    if not isinstance(p, dict) or not re.fullmatch(r"[\w.\-/]+", str(p.get("id", ""))):
        raise AIError("Paper inválido.")
    return p


# ---------------------------------------------------------------- buscador (arXiv e INSPIRE-HEP)

SEARCH_PAGE = 50
SEARCH_TTL = 10 * 60
_search_cache = {}
_arxiv_api_last = [0.0]
INSPIRE_FIELDS = ("titles.title,authors.full_name,abstracts.value,arxiv_eprints,citation_count,"
                  "publication_info.journal_title,publication_info.journal_volume,publication_info.year,"
                  "earliest_date,control_number")


def parse_arxiv_api(xml_bytes):
    root = ET.fromstring(xml_bytes)
    total = int(root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults", "0") or 0)
    out = []
    for e in root.findall("a:entry", NS):
        url = e.findtext("a:id", "", NS)
        if "/abs/" not in url:
            continue  # arXiv devuelve los errores como una entrada sin paper
        out.append({
            "id": re.sub(r"v\d+$", "", url.split("/abs/")[-1]),
            "title": clean(e.findtext("a:title", "", NS)),
            "authors": [clean(a.findtext("a:name", "", NS)) for a in e.findall("a:author", NS)],
            "abstract": clean(e.findtext("a:summary", "", NS)),
            "categories": [c.get("term") for c in e.findall("a:category", NS)],
            "type": "arxiv",
            "date": e.findtext("a:published", "", NS)[:10],
            "journal": clean(e.findtext("arxiv:journal_ref", "", NS)),
        })
    return total, out


def search_arxiv(q, author, cat, y_from, y_to, sort, start):
    parts = []
    for term in re.findall(r'"[^"]+"|[^\s"]+', re.sub(r"[()\[\]:]", " ", q)):
        parts.append(f"(ti:{term} OR abs:{term})")
    parts += [f"au:{w}" for w in re.sub(r"[()\[\]:\"]", " ", author).split()]
    if cat:
        parts.append(f"cat:{cat}")
    if y_from or y_to:
        parts.append(f"submittedDate:[{y_from or 1991}01010000 TO {y_to or 2100}12312359]")
    if not parts:
        raise UserError("Escribí algo para buscar.")
    wait = 3 - (time.time() - _arxiv_api_last[0])  # arXiv pide al menos 3 s entre consultas
    if wait > 0:
        time.sleep(wait)
    _arxiv_api_last[0] = time.time()
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
        "search_query": " AND ".join(parts), "start": start, "max_results": SEARCH_PAGE,
        "sortBy": "submittedDate" if sort == "recent" else "relevance", "sortOrder": "descending"})
    total, papers = parse_arxiv_api(fetch(url))
    return {"total": total, "papers": papers, "skipped": 0}


def inspire_query(q, sort="mostrecent", page=1, size=SEARCH_PAGE):
    url = "https://inspirehep.net/api/literature?" + urllib.parse.urlencode(
        {"q": q, "sort": sort, "page": page, "size": size, "fields": INSPIRE_FIELDS})
    hits = json.loads(fetch(url))["hits"]
    return hits["total"], [h["metadata"] for h in hits["hits"]]


def inspire_paper(m):
    ep = (m.get("arxiv_eprints") or [None])[0]
    if not ep:
        return None  # sin arXiv no se puede explicar ni chatear
    authors = []
    for a in m.get("authors", []):
        last, _, first = a.get("full_name", "").partition(", ")
        authors.append(f"{first} {last}".strip())
    pub = (m.get("publication_info") or [{}])[0]
    journal = " ".join(str(pub[k]) for k in ("journal_title", "journal_volume") if pub.get(k))
    if journal and pub.get("year"):
        journal += f" ({pub['year']})"
    return {
        "id": ep["value"],
        "title": clean((m.get("titles") or [{}])[0].get("title", "")),
        "authors": authors,
        "abstract": clean((m.get("abstracts") or [{}])[0].get("value", "")),
        "categories": ep.get("categories", []),
        "type": "inspire",
        "date": str(m.get("earliest_date", ""))[:10],
        "cites": m.get("citation_count", 0),
        "journal": journal,
    }


def search_inspire(q, author, cat, y_from, y_to, sort, start, rel=None, rel_id=None):
    if rel:
        _, found = inspire_query(f"arxiv:{rel_id}", size=1)
        if not found:
            raise UserError(f"El paper {rel_id} todavía no está en INSPIRE (suele tardar unos días en aparecer).")
        query = f"{'refersto' if rel == 'citations' else 'citedby'}:recid:{found[0]['control_number']}"
    else:
        parts = [f"({q})"] if q else []
        if author:
            parts.append(f"a {author}")
        if cat:
            parts.append(f"primarch {cat}")
        if y_from or y_to:
            parts.append(f"de {y_from or 1900}->{y_to or 2100}")
        if not parts:
            raise UserError("Escribí algo para buscar.")
        query = " and ".join(parts)
    order = {"recent": "mostrecent", "cited": "mostcited"}.get(sort, "bestmatch")
    total, metas = inspire_query(query, order, start // SEARCH_PAGE + 1)
    papers = [x for x in map(inspire_paper, metas) if x]
    return {"total": total, "papers": papers, "skipped": len(metas) - len(papers)}


def search(params):
    g = lambda k: (params.get(k, [""])[0] or "").strip()
    src, start = g("src"), int(g("start") or 0)
    for y in (g("from"), g("to")):
        if y and not re.fullmatch(r"\d{4}", y):
            raise UserError("Los años tienen que tener 4 dígitos.")
    if g("cat") and not re.fullmatch(r"[\w.\-]+", g("cat")):
        raise UserError("Categoría inválida.")
    if g("rel") and not re.fullmatch(r"[\w.\-/]+", g("id")):
        raise UserError("Paper inválido.")
    key = json.dumps(sorted((k, v) for k, v in params.items()))
    with _lock:
        hit = _search_cache.get(key)
        if hit and time.time() - hit[0] < SEARCH_TTL:
            return hit[1]
    args = (g("q"), g("author"), g("cat"), g("from"), g("to"), g("sort"), start)
    try:
        out = search_inspire(*args, rel=g("rel"), rel_id=g("id")) if src == "inspire" else search_arxiv(*args)
    except (urllib.error.URLError, TimeoutError, ET.ParseError, ValueError) as ex:
        raise UserError(f"{'INSPIRE' if src == 'inspire' else 'arXiv'} no respondió bien ({ex}). Probá de nuevo en un rato.")
    for p in out["papers"]:
        p["abs"] = f"https://arxiv.org/abs/{p['id']}"
        p["pdf"] = f"https://arxiv.org/pdf/{p['id']}"
    with _lock:
        _search_cache[key] = (time.time(), out)
    return out


# ---------------------------------------------------------------- servidor

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        """Solo pedidos de esta computadora. El Host evita que una página web maliciosa
        llegue al programa vía DNS rebinding, y el encabezado propio en los POST evita
        que otra página los dispare desde el navegador (CSRF)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("localhost", "127.0.0.1", "::1"):
            return False
        return self.command != "POST" or self.headers.get("X-Arxiv-Times") == "1"

    def do_GET(self):
        if not self.allowed():
            return self.send(403, "prohibido", "text/plain")
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            self.send(200, PAGE, "text/html; charset=utf-8")
        elif u.path == "/api/state":
            self.send(200, json.dumps(load_state()))
        elif u.path == "/api/ai/digest":
            try:
                path, _, _ = digest_path(parse_cats(qs.get("cats", [""])[0]))
                self.send(200, json.dumps({"digest": read_json(path)}))
            except Exception as ex:
                self.send(502, json.dumps({"error": str(ex)}))
        elif u.path == "/api/ai/chat":
            pid = qs.get("id", [""])[0]
            self.send(200, json.dumps(read_json(chat_path(pid), {"messages": []})))
        elif u.path == "/api/search":
            try:
                self.send(200, json.dumps(search(qs)))
            except UserError as ex:
                self.send(400, json.dumps({"error": str(ex)}))
            except Exception as ex:
                self.send(502, json.dumps({"error": f"Error inesperado: {ex}"}))
        elif u.path == "/api/papers":
            cats = parse_cats(qs.get("cats", [""])[0])
            if not cats:
                return self.send(400, '{"error":"Agregá al menos una categoría."}')
            try:
                self.send(200, json.dumps(get_papers(cats, bool(qs.get("refresh")))))
            except Exception as ex:
                self.send(502, json.dumps({"error": f"No pude descargar de arXiv: {ex}"}))
        else:
            self.send(404, "no encontrado", "text/plain")

    def do_POST(self):
        if not self.allowed():
            return self.send(403, json.dumps({"error": "Pedido no permitido."}))
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError as ex:
            return self.send(400, json.dumps({"error": str(ex)}))
        try:
            if self.path == "/api/state":
                out = save_state(body)
            elif self.path == "/api/ai/digest":
                out = make_digest(parse_cats(",".join(body.get("cats", []))), body.get("model"), bool(body.get("refresh")))
            elif self.path == "/api/ai/explain":
                out = explain(valid_paper(body.get("paper")), body.get("model"), bool(body.get("refresh")))
            elif self.path == "/api/ai/chat":
                msg = str(body.get("message", "")).strip()
                if not msg:
                    raise AIError("Mensaje vacío.")
                out = chat(valid_paper(body.get("paper")), msg, body.get("model"))
            elif self.path == "/api/ai/chat/reset":
                path = chat_path(str(body.get("id", "")))
                if os.path.exists(path):
                    os.remove(path)
                out = {"messages": []}
            else:
                return self.send(404, "no encontrado", "text/plain")
            self.send(200, json.dumps(out))
        except UserError as ex:
            self.send(400, json.dumps({"error": str(ex)}))
        except Exception as ex:
            self.send(502, json.dumps({"error": f"Error inesperado: {ex}"}))


def parse_cats(raw):
    return [c for c in raw.split(",") if re.fullmatch(r"[\w.\-]+", c)]


PAGE = r"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arxiv Times</title>
<meta name="theme-color" content="#b31b1b">
<script>window.MathJax={tex:{inlineMath:[['$','$'],['\\(','\\)']]},svg:{fontCache:'global'},startup:{typeset:false}};</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<style>
:root{--bg:#faf8f5;--card:#fff;--fg:#1d1d1f;--mut:#6b6b70;--line:#e6e2dc;--acc:#b31b1b;--hl:#fff1a8;--chip:#f0ece6;--good:#1a7f37}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1e1e21;--fg:#ececee;--mut:#9a9aa2;--line:#2e2e33;--acc:#ff6b6b;--hl:#5c4d00;--chip:#2a2a2f;--good:#4ac26b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,system-ui,"Segoe UI",Roboto,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px}
.wrap{max-width:860px;margin:0 auto}
.top{display:flex;align-items:center;gap:8px}
h1{margin:0;font-size:20px;flex:1}h1 b{color:var(--acc)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
input,select,button{font:inherit;font-size:14px;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
input[type=search]{flex:1;min-width:150px}
button{cursor:pointer}button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.tabs button{border-radius:99px}
#settings{display:none;border:1px solid var(--line);border-radius:12px;padding:12px;margin-top:10px;background:var(--card)}
#settings.open{display:block}
#settings h3{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:10px 0 4px}
#settings h3:first-child{margin-top:0}
#settings p{font-size:13px;color:var(--mut);margin:4px 0 0}
.chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chip{display:inline-flex;align-items:center;gap:4px;background:var(--chip);border-radius:99px;padding:2px 10px;font-size:13px}
.chip .x{cursor:pointer;opacity:.6;border:0;background:none;padding:0 0 0 2px;font-size:13px}
.meta{color:var(--mut);font-size:13px;margin-top:8px}
main{padding:4px 16px 80px}
article{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:12px 0;scroll-margin-top:220px}
article.read{opacity:.55}article.cur{outline:2px solid var(--acc);outline-offset:1px}
article h2{font-size:17px;margin:2px 0 4px;cursor:pointer;line-height:1.35}
.au{color:var(--mut);font-size:14px}.au a{color:inherit;text-decoration:none;cursor:pointer}.au a:hover{text-decoration:underline}
.au a.followed{color:var(--good);font-weight:600}
.abs{margin-top:8px;font-size:15px;display:none}article.open .abs{display:block}
.tags{margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.tags a,.tags button{font-size:13px;padding:3px 9px;text-decoration:none;color:inherit;border:1px solid var(--line);border-radius:6px;background:transparent}
.tags button.star{color:var(--acc)}
.head{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);display:flex;gap:8px;flex-wrap:wrap}
.head .type{color:var(--acc);font-weight:600}
.why{font-size:12px;color:var(--good);margin-top:4px}
mark{background:var(--hl);color:inherit;border-radius:2px}
.empty{text-align:center;color:var(--mut);padding:40px}
#help{position:fixed;inset:0;background:#0007;display:none;align-items:center;justify-content:center;z-index:10}
#help.open{display:flex}
#help div{background:var(--card);border-radius:12px;padding:20px 24px;max-width:360px;width:calc(100% - 32px)}
#help table{width:100%;font-size:14px;border-collapse:collapse}#help td{padding:3px 0}
kbd{font:12px ui-monospace,monospace;background:var(--chip);border:1px solid var(--line);border-radius:4px;padding:1px 6px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:12px 0}
.md h2{font-size:18px;margin:16px 0 6px}.md h2:first-child,.md h3:first-child{margin-top:0}.md h3{font-size:15px;margin:12px 0 4px}
.md p{margin:6px 0}.md ul,.md ol{padding-left:22px;margin:6px 0}.md li{margin:3px 0}
.md a.pid{color:var(--acc);text-decoration:none;font-variant-numeric:tabular-nums;white-space:nowrap}.md a.pid:hover{text-decoration:underline}
.md pre{white-space:pre-wrap}
.aibox{margin-top:10px;border-left:3px solid var(--acc);padding:10px 14px;background:var(--chip);border-radius:0 8px 8px 0;font-size:15px}
.small{font-size:12px;color:var(--mut)}
.linkbtn{border:0;background:none;color:var(--acc);padding:0;font-size:12px;cursor:pointer;text-decoration:underline}
.areas{width:100%;border-collapse:collapse;font-size:14px;margin:8px 0}
.areas tr{cursor:pointer}.areas tr:hover td{background:var(--chip)}
.areas td{padding:5px 6px;border-bottom:1px solid var(--line)}.areas td.n{text-align:right;width:36px;font-variant-numeric:tabular-nums}
.bar{height:8px;background:var(--acc);border-radius:4px;opacity:.7;min-width:2px}
body[data-view=digest] .listonly,body[data-view=search] .listonly{display:none}
.searchonly{display:none}body[data-view=search] .searchonly{display:block}
.head .cites{color:var(--good);font-weight:600}
#chat{position:fixed;inset:0;background:#0007;display:none;z-index:20;align-items:center;justify-content:center}
#chat.open{display:flex}
#chat .win{background:var(--bg);width:min(780px,100%);height:min(90vh,100%);border-radius:12px;display:flex;flex-direction:column;overflow:hidden}
#chat .bar2{padding:12px 16px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:flex-start}
#chat .bar2 div{flex:1;min-width:0}
#chatLog{flex:1;overflow:auto;padding:8px 16px}
.msg{margin:10px 0;padding:10px 12px;border-radius:10px;max-width:92%}
.msg.user{background:var(--acc);color:#fff;margin-left:auto;white-space:pre-wrap;width:fit-content}
.msg.assistant{background:var(--card);border:1px solid var(--line)}
#chatForm{display:flex;gap:8px;padding:10px 16px;border-top:1px solid var(--line)}
#chatIn{flex:1;font:inherit;font-size:15px;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;resize:none;height:64px}
@media (max-width:600px){.kb{display:none}header{position:static}article{scroll-margin-top:12px}#chat .win{height:100%;border-radius:0}}
</style></head><body>
<header><div class="wrap">
  <div class="top">
    <h1><b>Arxiv</b> Times</h1>
    <button id="reload" title="Volver a descargar">↻</button>
    <button id="gear" title="Preferencias">⚙</button>
    <button id="helpb" class="kb" title="Atajos de teclado">?</button>
  </div>
  <div id="settings">
    <h3>Categorías</h3>
    <div class="chips" id="cats"></div>
    <div class="row"><input id="newcat" placeholder="ej. hep-ph, astro-ph.CO, math-ph"><button data-add="cats">Agregar</button></div>
    <h3>Palabras clave</h3>
    <div class="chips" id="keywords"></div>
    <div class="row"><input id="newkeywords" placeholder="ej. black hole, holography"><button data-add="keywords">Agregar</button></div>
    <p>Se resaltan y suben los papers que las mencionan.</p>
    <h3>Autores que seguís</h3>
    <div class="chips" id="authors"></div>
    <div class="row"><input id="newauthors" placeholder="ej. Maldacena"><button data-add="authors">Agregar</button></div>
    <p>También podés hacer clic en el nombre de un autor en un paper para seguirlo.</p>
    <h3>Asistente de IA</h3>
    <div class="row"><select id="aiModel"><option value="sonnet">Sonnet (recomendado)</option><option value="opus">Opus (más capaz, usa más de tu plan)</option><option value="haiku">Haiku (más rápido, usa menos de tu plan)</option></select></div>
    <p>El resumen del día, las explicaciones y el chat usan tu plan de Claude a través de Claude Code.</p>
  </div>
  <div class="row tabs">
    <button id="tabToday" class="on">Hoy</button><button id="tabFavs">★ Me interesa <span id="nfav"></span></button><button id="tabToread">📖 Para leer <span id="ntoread"></span></button><button id="tabDigest">🧠 Resumen del día</button><button id="tabSearch">🔎 Buscar</button>
  </div>
  <div class="searchonly">
    <div class="row">
      <select id="sSrc"><option value="arxiv">arXiv</option><option value="inspire">INSPIRE-HEP</option></select>
      <input type="search" id="sQ" placeholder="Palabras del título o abstract…" style="flex:1;min-width:180px">
    </div>
    <div class="row">
      <input id="sAu" placeholder="Autor (ej. Maldacena)" style="flex:1;min-width:140px">
      <input id="sCat" placeholder="Categoría (ej. hep-th)" style="width:150px">
      <input id="sFrom" inputmode="numeric" placeholder="Desde (año)" style="width:110px">
      <input id="sTo" inputmode="numeric" placeholder="Hasta (año)" style="width:110px">
      <select id="sSort"></select>
      <button id="sGo" class="on">Buscar</button>
    </div>
    <div class="meta" id="sHelp"></div>
  </div>
  <div class="row listonly">
    <input type="search" id="q" placeholder="Buscar título, abstract, autor, id…">
    <select id="type"><option value="">Todos</option><option value="new" selected>Nuevos</option><option value="cross">Cross-lists</option><option value="replace">Reemplazos</option></select>
    <select id="sort"><option value="rel">Más relevantes primero</option><option value="arxiv">Orden de arXiv</option></select>
  </div>
  <div class="row listonly">
    <button id="unread">Ocultar leídos</button>
    <button id="onlyRel">Solo relevantes</button>
    <button id="markAll">Marcar visibles como leídos</button>
  </div>
  <div class="meta" id="meta"></div>
</div></header>
<main class="wrap" id="list"><div class="empty">Cargando…</div></main>
<div id="help"><div>
  <h3 style="margin-top:0">Atajos de teclado</h3>
  <table>
    <tr><td><kbd>j</kbd> / <kbd>k</kbd></td><td>siguiente / anterior</td></tr>
    <tr><td><kbd>o</kbd> o <kbd>Enter</kbd></td><td>abrir / cerrar abstract</td></tr>
    <tr><td><kbd>s</kbd></td><td>me interesa</td></tr>
    <tr><td><kbd>l</kbd></td><td>para leer</td></tr>
    <tr><td><kbd>m</kbd></td><td>leído / no leído</td></tr>
    <tr><td><kbd>e</kbd></td><td>explicar el paper (IA)</td></tr>
    <tr><td><kbd>c</kbd></td><td>chat sobre el paper (IA)</td></tr>
    <tr><td><kbd>p</kbd></td><td>abrir PDF</td></tr>
    <tr><td><kbd>a</kbd></td><td>abrir página del paper</td></tr>
    <tr><td><kbd>u</kbd></td><td>ocultar / mostrar leídos</td></tr>
    <tr><td><kbd>/</kbd></td><td>buscar</td></tr>
    <tr><td><kbd>?</kbd></td><td>esta ayuda</td></tr>
  </table>
</div></div>
<div id="chat"><div class="win">
  <div class="bar2"><div><b>💬 Chat sobre el paper</b><div class="small" id="chatTitle"></div></div>
    <button id="chatReset" title="Empezar de nuevo">Borrar</button><button id="chatClose" title="Cerrar (Esc)">✕</button></div>
  <div id="chatLog"></div>
  <form id="chatForm"><textarea id="chatIn" placeholder="Preguntá algo sobre el paper… (Enter envía, Shift+Enter nueva línea)"></textarea><button id="chatSend">Enviar</button></form>
</div></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let S={cats:[],keywords:[],authors:[],read:[],favs:{},toread:{},aiModel:'sonnet'}, readSet=new Set();
let data=null, view='today', hideRead=false, onlyRel=false, cur=-1, shown=[];

// ---------- estado (se guarda en la Mac)
let saveTimer=null, pending={};
function save(keys){keys.forEach(k=>pending[k]=S[k]);clearTimeout(saveTimer);
  saveTimer=setTimeout(()=>{const body=JSON.stringify(pending);pending={};
    fetch('/api/state',{method:'POST',body,headers:{'X-Arxiv-Times':'1'}}).catch(()=>{})},300)}
function setRead(id,v){v?readSet.add(id):readSet.delete(id);S.read=[...readSet];save(['read'])}

// ---------- relevancia
const STOP=new Set('this that with from these those their there which where when what have been were will would could should into onto than then them they also such some more most other only over under using used based show shows paper study studies work results result present presents approach method methods model models theory theories case cases find found well both each between while within without first second new non can its our we the and for are not via one two given general further here however may large small high low order terms term form function functions recent recently provide provides propose proposed obtain obtained discuss consider considered analysis either neither whether thus therefore since whereas although though jointly increase increases decrease decreases demonstrate demonstrates allow allows allowing lead leads leading various several many much even still very rather same different possible particular particularly respectively additionally moreover finally novel framework explore explores investigate investigates investigated develop developed derive derived describe describes'.split(' '));
function toks(s){return (s.toLowerCase().match(/[a-z][a-z\-]{3,}/g)||[]).filter(w=>!STOP.has(w))}
// Raíz aproximada: "holographic"/"holography" -> "holograph", "holes" -> "hole"
const SUF=['ically','ical','ies','ions','ion','ity','ic','y','s'];
function stem(w){for(const suf of SUF)if(w.endsWith(suf)&&w.length-suf.length>=4&&!(suf==='s'&&w.endsWith('ss')))return w.slice(0,-suf.length);return w}
// Términos de un paper: las palabras del título pesan 2, las del abstract 1.
const termCache=new Map();
function termsOf(p){let t=termCache.get(p.id);if(t)return t;t=new Map();
  for(const [txt,w] of [[p.title,2],[p.abstract,1]])for(const word of toks(txt)){const k=stem(word),o=t.get(k);
    if(!o)t.set(k,{w,word});else if(o.w<3&&w!==o.w)o.w=3}
  termCache.set(p.id,t);return t}
// Modelo: perfil TF-IDF de lo que te interesa; el IDF sale de los papers del día,
// así una palabra que aparece en medio listado ("gravity") casi no cuenta.
let df=new Map(), nDay=0, prof=new Map(), profNorm=0, sims=new Map(), simCut=Infinity;
const SIM_MIN=0.1, SIM_TOP=0.1;  // marca como mucho el 10% del día, y solo si la similitud supera 0,1
const idfOf=k=>Math.log((nDay+1)/((df.get(k)||0)+1))+1;
function simOf(p){let dot=0,n2=0;const hits=[];
  for(const [k,v] of termsOf(p)){const x=v.w*idfOf(k);n2+=x*x;const y=prof.get(k);if(y){dot+=x*y;hits.push([x*y,v.word])}}
  hits.sort((a,b)=>b[0]-a[0]);
  return {sim:n2&&profNorm?dot/(Math.sqrt(n2)*profNorm):0,words:hits.slice(0,3).map(h=>h[1])}}
function buildModel(){
  const day=data?.papers||[];nDay=day.length;df=new Map();
  for(const p of day)for(const k of termsOf(p).keys())df.set(k,(df.get(k)||0)+1);
  prof=new Map();for(const p of Object.values(S.favs))for(const [k,v] of termsOf(p))prof.set(k,(prof.get(k)||0)+v.w);
  profNorm=0;for(const [k,v] of prof){const x=v*idfOf(k);prof.set(k,x);profNorm+=x*x}profNorm=Math.sqrt(profNorm);
  sims=new Map();const all=[];
  if(prof.size)for(const p of day){if(S.favs[p.id])continue;const r=simOf(p);sims.set(p.id,r);all.push(r.sim)}
  all.sort((a,b)=>b-a);simCut=all.length?Math.max(SIM_MIN,all[Math.max(0,Math.ceil(all.length*SIM_TOP)-1)]):Infinity}
function relevance(p){
  const text=(p.title+' '+p.abstract).toLowerCase(), why=[];let s=0;
  for(const k of S.keywords)if(text.includes(k.toLowerCase())){s+=3;why.push('“'+k+'”')}
  for(const a of S.authors){const al=a.toLowerCase();if(p.authors.some(x=>x.toLowerCase().includes(al))){s+=5;why.push(a)}}
  const r=sims.get(p.id);
  if(r&&r.sim>=simCut){s+=2+r.sim;why.push('parecido a lo que te interesa ('+r.words.join(', ')+')')}
  return {s,why}}

// ---------- resaltado
function hlRe(){const k=[...S.keywords,...S.authors].filter(Boolean);if(!k.length)return null;
  return new RegExp('('+k.map(x=>esc(x).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|')+')','gi')}
function hl(s,re){const t=esc(s);return re?t.replace(re,'<mark>$1</mark>'):t}

// ---------- preferencias
function drawSettings(){
  for(const k of ['cats','keywords','authors'])
    $(k).innerHTML=S[k].map((v,i)=>`<span class="chip">${esc(v)}<button class="x" data-del="${k}" data-i="${i}" title="Quitar">✕</button></span>`).join('')||'<span class="meta" style="margin:0">—</span>'}
document.addEventListener('click',e=>{
  const d=e.target.closest('[data-del]');
  if(d){const k=d.dataset.del;S[k].splice(+d.dataset.i,1);save([k]);drawSettings();k==='cats'?load():(buildModel(),render())}
  const a=e.target.closest('[data-add]');
  if(a){const k=a.dataset.add,inp=$('new'+(k==='cats'?'cat':k));
    for(const v of inp.value.split(',').map(s=>s.trim()).filter(Boolean))if(!S[k].includes(v))S[k].push(v);
    inp.value='';save([k]);drawSettings();k==='cats'?load():render()}});
['newcat','newkeywords','newauthors'].forEach(id=>$(id).addEventListener('keydown',e=>{
  if(e.key==='Enter')$(id).parentElement.querySelector('[data-add]').click()}));

// ---------- datos
async function load(refresh){
  if(!S.cats.length){data=null;$('list').innerHTML='<div class="empty">Agregá al menos una categoría en ⚙.</div>';$('meta').textContent='';return}
  $('list').innerHTML='<div class="empty">Descargando de arXiv…</div>';
  try{const r=await fetch('/api/papers?cats='+encodeURIComponent(S.cats.join(','))+(refresh?'&refresh=1':''));
    data=await r.json();if(data.error)throw new Error(data.error);cur=-1;buildModel();render();loadDigest()}
  catch(e){data=null;$('list').innerHTML=`<div class="empty">${esc(e.message)}</div>`}}

function render(keepOrder){
  document.body.dataset.view=view;
  if(view==='digest')return renderDigest();
  const re=hlRe(), q=$('q').value.trim().toLowerCase(), ty=$('type').value;
  const src=view==='today'?(data?.papers||[]):view==='search'?search.results:Object.values(S[view]).reverse();
  let list=src.map((p,i)=>({p,i,r:relevance(p)})).filter(({p,r})=>{
    if(view==='today'&&ty&&!p.type.startsWith(ty))return false;
    if(view==='today'&&areaFilter&&digest?.areas[p.id]!==areaFilter)return false;
    if(hideRead&&view==='today'&&readSet.has(p.id))return false;
    if(view==='search')return true;
    if(onlyRel&&r.s<=0)return false;
    if(q&&!(p.title+' '+p.abstract+' '+p.authors.join(' ')+' '+p.id).toLowerCase().includes(q))return false;
    return true});
  if(keepOrder){const pos=new Map(shown.map((p,i)=>[p.id,i]));list.sort((a,b)=>(pos.get(a.p.id)??1e9)-(pos.get(b.p.id)??1e9)||a.i-b.i)}
  else if(view!=='search'&&$('sort').value==='rel')list.sort((a,b)=>b.r.s-a.r.s||a.i-b.i);
  shown=list.map(x=>x.p);
  $('nfav').textContent=Object.keys(S.favs).length||'';$('ntoread').textContent=Object.keys(S.toread).length||'';
  if(view==='today'&&data){
    const total=data.papers.length, unread=data.papers.filter(p=>!readSet.has(p.id)).length;
    const date=data.papers[0]?.date||'';
    $('meta').innerHTML=esc(`Anuncio del ${date} · mostrando ${list.length} de ${total} · ${unread} sin leer · actualizado ${data.fetched}`)+
      (areaFilter?` · Área: <b>${esc(areaFilter)}</b> <button class="linkbtn" id="clearArea">quitar ✕</button>`:'')}
  else if(view==='favs')$('meta').textContent=`${list.length} papers que te interesan · se usan para encontrar parecidos`;
  else if(view==='toread')$('meta').textContent=`${list.length} papers para leer`;
  else $('meta').textContent=search.params?`${search.label||('Búsqueda en '+(search.params.src==='inspire'?'INSPIRE-HEP':'arXiv'))} · ${search.total.toLocaleString('es-AR')} resultados`:'';
  const fa=S.authors.map(a=>a.toLowerCase());
  $('list').innerHTML=list.length?list.map(({p,r},n)=>`
  <article data-n="${n}" class="${readSet.has(p.id)?'read':''} ${n===cur?'cur':''}">
    <div class="head"><span class="type">${esc(p.type)}</span><span>${esc(p.id)}</span>${view==='search'&&p.date?`<span>${esc(p.date)}</span>`:''}<span>${p.categories.map(esc).join(' · ')}</span>${p.journal?`<span>${esc(p.journal)}</span>`:''}${p.cites!=null?`<span class="cites">${p.cites} citas</span>`:''}</div>
    <h2>${hl(p.title,re)}</h2>
    <div class="au">${p.authors.slice(0,10).map(a=>`<a data-author="${esc(a)}" class="${fa.some(f=>a.toLowerCase().includes(f))?'followed':''}" title="Seguir a este autor">${hl(a,re)}</a>`).join(', ')}${p.authors.length>10?` y ${p.authors.length-10} más`:''}</div>
    ${r.why.length?`<div class="why">★ ${r.why.map(esc).join(' · ')}</div>`:''}
    <div class="abs">${hl(p.abstract,re)}</div>
    <div class="tags">
      <button data-act="open">Abstract</button>
      <a href="${esc(p.pdf)}" target="_blank" rel="noopener">PDF</a>
      <a href="${esc(p.abs)}" target="_blank" rel="noopener">arXiv</a>
      <button data-act="star" class="star">${S.favs[p.id]?'★ Me interesa':'☆ Me interesa'}</button>
      <button data-act="toread">${S.toread[p.id]?'📖 En Para leer':'＋ Para leer'}</button>
      <button data-act="read">${readSet.has(p.id)?'Marcar no leído':'Marcar leído'}</button>
      <button data-act="explain">${explainOpen.has(p.id)?'🧠 Ocultar explicación':'🧠 Explicar'}</button>
      <button data-act="chat">💬 Chat</button>
      <button data-act="citations" title="Papers que citan a este (INSPIRE)">📈 Citas</button>
      <button data-act="references" title="Papers que cita este (INSPIRE)">📚 Referencias</button>
    </div>
    ${explainOpen.has(p.id)?explainBox(p.id):''}
  </article>`).join(''):(view==='search'?'':`<div class="empty">${{favs:'Todavía no marcaste papers que te interesen.',toread:'No tenés papers para leer.'}[view]||'Nada para mostrar con estos filtros.'}</div>`);
  if(view==='search')$('list').insertAdjacentHTML('beforeend',searchFooter());
  if(window.MathJax?.typesetPromise)MathJax.typesetPromise([$('list')]).catch(()=>{});
}

// ---------- acciones sobre un paper
function artOf(n){return document.querySelector(`article[data-n="${n}"]`)}
function toggleOpen(n){const art=artOf(n),p=shown[n];if(!art)return;art.classList.toggle('open');
  if(art.classList.contains('open')&&!readSet.has(p.id)){setRead(p.id,true);art.classList.add('read');
    art.querySelector('[data-act=read]').textContent='Marcar no leído'}}
function toggleStar(n){const p=shown[n];if(S.favs[p.id])delete S.favs[p.id];else S.favs[p.id]=p;save(['favs']);buildModel();rerenderKeep(n)}
function toggleToread(n){const p=shown[n];if(S.toread[p.id])delete S.toread[p.id];else S.toread[p.id]=p;save(['toread']);rerenderKeep(n)}
function toggleRead(n){const p=shown[n];setRead(p.id,!readSet.has(p.id));rerenderKeep(n)}
function rerenderKeep(n){const open=[...document.querySelectorAll('article.open')].map(a=>shown[+a.dataset.n]?.id);
  const id=shown[n]?.id;render(true);const k=shown.findIndex(p=>p.id===id);if(k>=0)cur=k;
  shown.forEach((p,i)=>{if(open.includes(p.id))artOf(i)?.classList.add('open')});markCur(false)}
function markCur(scroll=true){document.querySelectorAll('article.cur').forEach(a=>a.classList.remove('cur'));
  const a=artOf(cur);if(a){a.classList.add('cur');if(scroll)a.scrollIntoView({block:'start',behavior:'smooth'})}}

$('list').addEventListener('click',e=>{
  const art=e.target.closest('article');if(!art)return;const n=+art.dataset.n;cur=n;markCur(false);
  const au=e.target.closest('[data-author]');
  if(au){const name=au.dataset.author;const i=S.authors.indexOf(name);
    if(i>=0){if(confirm(`¿Dejar de seguir a ${name}?`)){S.authors.splice(i,1)}else return}
    else if(confirm(`¿Seguir a ${name}? Sus papers aparecerán primero.`))S.authors.push(name);else return;
    save(['authors']);drawSettings();rerenderKeep(n);return}
  if(e.target.closest('h2'))return toggleOpen(n);
  const act=e.target.closest('[data-act]')?.dataset.act;
  if(act==='open')toggleOpen(n);if(act==='star')toggleStar(n);if(act==='toread')toggleToread(n);if(act==='read')toggleRead(n);
  if(act==='explain')toggleExplain(n);if(act==='explainRetry')runExplain(shown[n],true);if(act==='chat')openChat(shown[n]);
  if(act==='citations'||act==='references')searchRelated(act,shown[n])});

// ---------- barra
function setView(v){view=v;$('tabToday').classList.toggle('on',v==='today');$('tabFavs').classList.toggle('on',v==='favs');$('tabToread').classList.toggle('on',v==='toread');$('tabDigest').classList.toggle('on',v==='digest');$('tabSearch').classList.toggle('on',v==='search');cur=-1;render();scrollTo(0,0)}
$('tabToday').onclick=()=>setView('today');$('tabFavs').onclick=()=>setView('favs');$('tabToread').onclick=()=>setView('toread');$('tabDigest').onclick=()=>setView('digest');$('tabSearch').onclick=()=>{setView('search');if(!search.params)$('sQ').focus()};
$('gear').onclick=()=>{$('settings').classList.toggle('open');$('gear').classList.toggle('on')};
$('helpb').onclick=()=>$('help').classList.add('open');$('help').onclick=()=>$('help').classList.remove('open');
$('reload').onclick=()=>load(true);
$('q').oninput=()=>{cur=-1;render()};$('type').onchange=render;$('sort').onchange=render;
$('unread').onclick=()=>{hideRead=!hideRead;$('unread').classList.toggle('on',hideRead);cur=-1;render()};
$('onlyRel').onclick=()=>{onlyRel=!onlyRel;$('onlyRel').classList.toggle('on',onlyRel);cur=-1;render()};
$('markAll').onclick=()=>{if(!shown.length||!confirm(`¿Marcar ${shown.length} papers como leídos?`))return;
  shown.forEach(p=>readSet.add(p.id));S.read=[...readSet];save(['read']);cur=-1;render()};

// ---------- teclado
document.addEventListener('keydown',e=>{
  if(e.metaKey||e.ctrlKey||e.altKey)return;
  if($('chat').classList.contains('open')){if(e.key==='Escape')closeChat();return}
  if(e.target.matches?.('input,select,textarea')){if(e.key==='Escape')e.target.blur();return}
  if($('help').classList.contains('open')){$('help').classList.remove('open');return}
  const k=e.key;
  if(k==='j'){cur=Math.min(shown.length-1,cur+1);markCur()}
  else if(k==='k'){cur=Math.max(0,cur-1);markCur()}
  else if(k==='/'){e.preventDefault();$(view==='search'?'sQ':'q').focus()}
  else if(k==='?'){$('help').classList.add('open')}
  else if(k==='u'){$('unread').click()}
  else if(cur>=0&&shown[cur]){
    if(k==='o'||k==='Enter')toggleOpen(cur);
    else if(k==='s')toggleStar(cur);
    else if(k==='l')toggleToread(cur);
    else if(k==='m')toggleRead(cur);
    else if(k==='e')toggleExplain(cur);
    else if(k==='c')openChat(shown[cur]);
    else if(k==='p')window.open(shown[cur].pdf,'_blank');
    else if(k==='a')window.open(shown[cur].abs,'_blank');
    else return}
  else return;
  e.preventDefault()});

// ---------- buscador
const search={params:null,results:[],total:0,skipped:0,state:'idle',err:'',label:''};
const SORTS={arxiv:[['relevance','Más relevantes'],['recent','Más recientes']],
             inspire:[['recent','Más recientes'],['cited','Más citados'],['relevance','Más relevantes']]};
const HELP={arxiv:'Busca en todo arXiv. Para una frase exacta usá comillas, ej. "black hole". Para el autor alcanza con el apellido.',
            inspire:'Busca en INSPIRE-HEP (física de altas energías), con cantidad de citas. En “Palabras” también podés usar la sintaxis de INSPIRE, ej. t holography and a maldacena. Solo se muestran los papers que están en arXiv.'};
function setSrc(src,sort){$('sSrc').value=src;$('sSort').innerHTML=SORTS[src].map(([v,t])=>`<option value="${v}">${t}</option>`).join('');
  if(sort)$('sSort').value=sort;$('sHelp').textContent=HELP[src]}
$('sSrc').onchange=()=>setSrc($('sSrc').value);setSrc('arxiv');
async function doSearch(more){
  if(!more){search.results=[];search.total=0;search.skipped=0;cur=-1}
  search.state='loading';render(true);
  try{const r=await api('/api/search?'+new URLSearchParams({...search.params,start:more?search.results.length+search.skipped:0}));
    const seen=new Set(search.results.map(p=>p.id));
    search.results.push(...r.papers.filter(p=>!seen.has(p.id)));search.total=r.total;search.skipped+=r.skipped;search.state='idle'}
  catch(e){search.state='error';search.err=e.message}
  if(view==='search')render(true)}
function runFormSearch(){
  search.params={src:$('sSrc').value,q:$('sQ').value.trim(),author:$('sAu').value.trim(),cat:$('sCat').value.trim(),
    from:$('sFrom').value.trim(),to:$('sTo').value.trim(),sort:$('sSort').value};
  search.label='';doSearch(false)}
function searchRelated(kind,p){
  search.params={src:'inspire',rel:kind,id:p.id,sort:'cited'};
  search.label=`${kind==='citations'?'Papers que citan a':'Referencias de'} “${p.title.length>70?p.title.slice(0,70)+'…':p.title}”`;
  setSrc('inspire','cited');setView('search');doSearch(false)}
function searchFooter(){
  if(search.state==='loading')return `<div class="empty">Buscando en ${search.params?.src==='inspire'?'INSPIRE-HEP':'arXiv'}…</div>`;
  if(search.state==='error')return `<div class="panel">⚠️ ${esc(search.err)}</div>`;
  if(!search.params)return '<div class="empty">Buscá papers en todo arXiv o en INSPIRE-HEP. Desde cualquier paper también podés ver quién lo cita (📈 Citas) y qué cita (📚 Referencias).</div>';
  if(!search.results.length)return '<div class="empty">No encontré papers con esa búsqueda.</div>';
  const left=search.total-search.results.length-search.skipped;
  return `<div class="empty">Mostrando ${search.results.length} de ${search.total.toLocaleString('es-AR')}${search.skipped?` · ${search.skipped} sin versión en arXiv no se muestran`:''}
    ${left>0?' · <button id="sMore">Cargar más</button>':''}</div>`}
document.addEventListener('click',e=>{if(e.target.id==='sMore')doSearch(true)});
$('sGo').onclick=runFormSearch;
['sQ','sAu','sCat','sFrom','sTo'].forEach(id=>$(id).addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();runFormSearch()}}));

// ---------- asistente de IA (usa Claude Code con tu plan)
let digest=null, digestState='idle', digestErr='', areaFilter=null, explains={}, explainOpen=new Set();
async function api(url,body){
  const r=await fetch(url,body?{method:'POST',body:JSON.stringify(body),headers:{'X-Arxiv-Times':'1'}}:undefined);
  let j;try{j=await r.json()}catch{throw new Error('Respuesta inválida del servidor ('+r.status+')')}
  if(!r.ok||j.error)throw new Error(j.error||('Error '+r.status));return j}
const paperLite=p=>({id:p.id,title:p.title,authors:p.authors,abstract:p.abstract});
function typeset(el){if(window.MathJax?.typesetPromise)MathJax.typesetPromise([el]).catch(()=>{})}
// Markdown con LaTeX: protejo las fórmulas antes de pasar por marked y convierto los ids de arXiv en links internos.
function md(src){
  const math=[];
  src=String(src||'').replace(/\$\$[\s\S]+?\$\$|\$[^$\n]+?\$/g,m=>{math.push(m);return `@@M${math.length-1}@@`});
  src=src.replace(/\[?\b(\d{4}\.\d{4,5})(?:v\d+)?\b\]?(?!\()/g,(m,id)=>`[${id}](#p-${id})`);
  let h=window.marked?marked.parse(src):'<p>'+esc(src).replace(/\n/g,'<br>')+'</p>';
  if(window.DOMPurify)h=DOMPurify.sanitize(h);
  h=h.replace(/<a href="#p-([\d.]+)">/g,'<a class="pid" href="#p-$1">');
  return h.replace(/@@M(\d+)@@/g,(_,i)=>esc(math[i]))}
document.addEventListener('click',e=>{
  const a=e.target.closest('a[href^="#p-"]');if(a){e.preventDefault();closeChat();goToPaper(a.getAttribute('href').slice(3))}
  if(e.target.id==='clearArea'){areaFilter=null;cur=-1;render()}});
function goToPaper(id){
  areaFilter=null;if(view!=='today')setView('today');
  let k=shown.findIndex(p=>p.id===id);
  if(k<0){$('q').value=id;$('type').value='';render();k=shown.findIndex(p=>p.id===id)}
  if(k<0)return;cur=k;markCur();const art=artOf(k);if(art&&!art.classList.contains('open'))toggleOpen(k)}

// explicación de un paper
function explainBox(id){const x=explains[id];
  if(!x||x.state==='loading')return '<div class="aibox"><span class="small">🧠 Leyendo el paper completo y preparando la explicación… suele tardar entre 30 segundos y 2 minutos.</span></div>';
  if(x.state==='error')return `<div class="aibox">⚠️ ${esc(x.error)} <button class="linkbtn" data-act="explainRetry">Reintentar</button></div>`;
  return `<div class="aibox md">${md(x.md)}<p class="small">Explicación generada con ${esc(x.model)} el ${esc(x.generated)} · <button class="linkbtn" data-act="explainRetry">Regenerar</button></p></div>`}
function toggleExplain(n){const p=shown[n];if(!p)return;
  if(explainOpen.has(p.id))explainOpen.delete(p.id);
  else{explainOpen.add(p.id);if(!explains[p.id]||explains[p.id].state==='error')runExplain(p,false)}
  rerenderKeep(n)}
function runExplain(p,refresh){
  explains[p.id]={state:'loading'};updateExplain(p.id);
  api('/api/ai/explain',{paper:paperLite(p),model:S.aiModel,refresh})
    .then(r=>explains[p.id]={state:'done',md:r.markdown,model:r.model,generated:r.generated})
    .catch(e=>explains[p.id]={state:'error',error:e.message})
    .finally(()=>updateExplain(p.id))}
function updateExplain(id){const art=artOf(shown.findIndex(p=>p.id===id));if(!art)return;
  art.querySelector('.aibox')?.remove();
  if(explainOpen.has(id)){art.insertAdjacentHTML('beforeend',explainBox(id));typeset(art.querySelector('.aibox'))}}

// resumen del día
async function loadDigest(){digest=null;digestState='idle';
  try{const r=await api('/api/ai/digest?cats='+encodeURIComponent(S.cats.join(',')));digest=r.digest}catch{}
  if(view==='digest')render()}
function runDigest(refresh){digestState='loading';render();
  api('/api/ai/digest',{cats:S.cats,model:S.aiModel,refresh})
    .then(r=>{digest=r;digestState='idle'}).catch(e=>{digestState='error';digestErr=e.message})
    .finally(()=>{if(view==='digest')render()})}
function renderDigest(){
  const L=$('list'), date=data?.papers[0]?.date||'';
  $('meta').textContent=`Resumen del anuncio del ${date} · ${S.cats.join(', ')}`;
  if(digestState==='loading'){L.innerHTML='<div class="panel"><b>🧠 Leyendo los títulos y abstracts de todos los papers del día…</b><p class="small">Suele tardar entre 1 y 3 minutos. Mientras tanto podés seguir usando la pestaña “Hoy”.</p></div>';return}
  if(!digest){
    L.innerHTML=`<div class="panel">${digestState==='error'?`<p>⚠️ ${esc(digestErr)}</p>`:''}
      <p>Claude lee los títulos y abstracts de todos los papers nuevos y cross-lists del día en tus categorías (${esc(S.cats.join(', '))}). Te dice qué temas fueron los más discutidos, cuántos papers hay de cada área y cuáles te pueden interesar.</p>
      <button id="digestGo" class="on">Generar resumen del día</button>
      <p class="small">Usa tu plan de Claude (modelo: ${esc(S.aiModel)}). El resumen queda guardado, así que alcanza con generarlo una vez por día.</p></div>`;
    $('digestGo').onclick=()=>runDigest(false);return}
  const counts={};for(const a of Object.values(digest.areas))counts[a]=(counts[a]||0)+1;
  const rows=Object.entries(counts).sort((a,b)=>b[1]-a[1]), max=rows[0]?.[1]||1;
  L.innerHTML=`${digestState==='error'?`<div class="panel">⚠️ ${esc(digestErr)}</div>`:''}
  <div class="panel md">${md(digest.markdown)}</div>
  <div class="panel"><h2 style="margin:0 0 4px;font-size:18px">Papers por área</h2>
    <p class="small">${digest.n} papers (nuevos y cross-lists), cada uno en su área principal. Hacé clic en un área para ver sus papers.</p>
    <table class="areas">${rows.map(([a,n])=>`<tr data-area="${esc(a)}"><td>${esc(a)}</td><td class="n">${n}</td><td style="width:35%"><div class="bar" style="width:${n/max*100}%"></div></td></tr>`).join('')}</table>
    <p class="small">Generado el ${esc(digest.generated)} con ${esc(digest.model)} · <button class="linkbtn" id="digestRe">Regenerar</button></p></div>`;
  L.querySelectorAll('[data-area]').forEach(tr=>tr.onclick=()=>{areaFilter=tr.dataset.area;$('type').value='';$('q').value='';setView('today')});
  $('digestRe').onclick=()=>{if(confirm('¿Generar el resumen de nuevo? Vuelve a usar tu plan de Claude.'))runDigest(true)};
  typeset(L)}

// chat sobre el PDF
let chatPaper=null, chatMsgs=[], chatBusy=false;
async function openChat(p){if(!p)return;chatPaper=p;chatMsgs=[];
  $('chatTitle').textContent=`${p.title} · ${p.id}`;$('chat').classList.add('open');drawChat('Cargando…');
  try{const r=await api('/api/ai/chat?id='+encodeURIComponent(p.id));if(chatPaper===p)chatMsgs=r.messages||[]}catch{}
  if(chatPaper===p){drawChat();$('chatIn').focus()}}
function closeChat(){$('chat').classList.remove('open')}
function drawChat(pending){const log=$('chatLog');
  log.innerHTML=(chatMsgs.length||pending?'':'<p class="small">Preguntá lo que quieras sobre este paper. La primera respuesta tarda un poco más porque Claude lee el paper completo; las siguientes son más rápidas y usan menos de tu plan.</p>')+
    chatMsgs.map(m=>m.role==='user'?`<div class="msg user">${esc(m.text)}</div>`:`<div class="msg assistant md">${md(m.text)}</div>`).join('')+
    (pending?`<div class="msg assistant small">${esc(pending)}</div>`:'');
  typeset(log);log.scrollTop=log.scrollHeight}
async function sendChat(){const t=$('chatIn').value.trim(), p=chatPaper;if(!t||chatBusy||!p)return;
  chatBusy=true;$('chatSend').disabled=true;$('chatIn').value='';
  chatMsgs.push({role:'user',text:t});drawChat(chatMsgs.length===1?'Leyendo el paper y pensando…':'Pensando…');
  try{const r=await api('/api/ai/chat',{paper:paperLite(p),message:t,model:S.aiModel});if(chatPaper===p){chatMsgs=r.messages;drawChat()}}
  catch(e){if(chatPaper===p){chatMsgs.pop();drawChat('⚠️ '+e.message);$('chatIn').value=t}}
  chatBusy=false;$('chatSend').disabled=false}
$('chatForm').onsubmit=e=>{e.preventDefault();sendChat()};
$('chatIn').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat()}});
$('chatClose').onclick=closeChat;
$('chat').addEventListener('click',e=>{if(e.target.id==='chat')closeChat()});
$('chatReset').onclick=async()=>{if(!chatPaper||chatBusy||!confirm('¿Borrar esta conversación?'))return;
  await api('/api/ai/chat/reset',{id:chatPaper.id}).catch(()=>{});chatMsgs=[];drawChat()};
$('aiModel').onchange=()=>{S.aiModel=$('aiModel').value;save(['aiModel'])};

// ---------- inicio
(async()=>{
  try{S=await (await fetch('/api/state')).json()}catch{}
  S.toread=S.toread||{};S.aiModel=S.aiModel||'sonnet';$('aiModel').value=S.aiModel;readSet=new Set(S.read);buildModel();drawSettings();load()})();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Lector diario de arXiv")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)  # solo esta computadora
    print(f"Arxiv Times en http://localhost:{args.port}")
    print(f"Tus datos se guardan en {STATE_FILE}")
    print("Ctrl+C para salir.")
    if not args.no_browser:
        webbrowser.open(f"http://localhost:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
