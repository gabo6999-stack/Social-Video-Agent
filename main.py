from dotenv import load_dotenv
load_dotenv()  # debe ser lo primero antes de cualquier os.environ.get()

import os
import json
import sqlite3
import requests
from datetime import datetime
from flask import Flask, request, jsonify, Response, stream_with_context
from requests.auth import HTTPBasicAuth
import anthropic

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BLOG_AGENT_URL = os.environ.get("BLOG_AGENT_URL", "https://agente-blogs-production.up.railway.app")
SEO_AGENT_URL  = os.environ.get("SEO_AGENT_URL",  "https://web-production-3743c.up.railway.app")

WC_URL    = os.environ.get("WC_STORE_URL", "").rstrip("/")
WC_KEY    = os.environ.get("WC_CONSUMER_KEY", "")
WC_SECRET = os.environ.get("WC_CONSUMER_SECRET", "")
DB_PATH   = os.environ.get("DB_PATH", "content.db")
MODEL     = "claude-sonnet-4-6"

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

# ─── BASE DE DATOS ────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS content_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            platform TEXT,
            content_type TEXT,
            content TEXT,
            product TEXT,
            created_at TEXT
        )
    """)
    con.commit()
    con.close()

init_db()


def save_messages(session_id, messages):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
    for m in messages:
        content = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])
        cur.execute(
            "INSERT INTO conversations (session_id, role, content, created_at) VALUES (?,?,?,?)",
            (session_id, m["role"], content, datetime.now().isoformat())
        )
    con.commit()
    con.close()


def load_messages(session_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT role, content FROM conversations WHERE session_id = ? ORDER BY id",
        (session_id,)
    )
    rows = cur.fetchall()
    con.close()
    messages = []
    for role, content in rows:
        try:
            parsed = json.loads(content)
            messages.append({"role": role, "content": parsed})
        except Exception:
            messages.append({"role": role, "content": content})
    return messages


# ─── HERRAMIENTAS ─────────────────────────────────────────────────────────────

def get_products(per_page=20):
    if not WC_URL:
        return {"error": "WC_STORE_URL no configurado"}
    try:
        r = requests.get(
            f"{WC_URL}/wp-json/wc/v3/products",
            auth=HTTPBasicAuth(WC_KEY, WC_SECRET),
            params={
                "per_page": per_page, "status": "publish",
                "_fields": "id,name,short_description,price,categories,tags,stock_status"
            },
            timeout=15
        )
        r.raise_for_status()
        return [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "price_mxn": p.get("price"),
                "short_description": p.get("short_description", "")[:250],
                "categories": [c["name"] for c in p.get("categories", [])],
                "tags": [t["name"] for t in p.get("tags", [])],
                "stock_status": p.get("stock_status"),
            }
            for p in r.json()
        ]
    except Exception as e:
        return {"error": str(e)}


def save_content(title, platform, content_type, content, product=""):
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute(
            "INSERT INTO content_library (title, platform, content_type, content, product, created_at) VALUES (?,?,?,?,?,?)",
            (title, platform, content_type, content, product, datetime.now().isoformat())
        )
        con.commit()
        item_id = cur.lastrowid
        con.close()
        return {"success": True, "id": item_id, "saved": title}
    except Exception as e:
        return {"error": str(e)}


def list_content(platform=None, content_type=None, limit=10):
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        query = "SELECT id, title, platform, content_type, product, created_at FROM content_library"
        params = []
        filters = []
        if platform:
            filters.append("platform = ?")
            params.append(platform)
        if content_type:
            filters.append("content_type = ?")
            params.append(content_type)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        cur.execute(query, params)
        rows = cur.fetchall()
        con.close()
        return [
            {
                "id": r[0], "title": r[1], "platform": r[2],
                "content_type": r[3], "product": r[4], "created_at": r[5]
            }
            for r in rows
        ]
    except Exception as e:
        return {"error": str(e)}


# ─── INTEGRACIÓN CON AGENTES HERMANOS ────────────────────────────────────────

def publicar_blog(topic=None, site_key="peptidosysuplementos"):
    try:
        payload = {"site_key": site_key}
        if topic:
            payload["topic"] = topic
        r = requests.post(f"{BLOG_AGENT_URL}/publish", json=payload, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def estado_agente_blogs():
    try:
        r = requests.get(f"{BLOG_AGENT_URL}/status", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def consultar_agente_seo(instruccion):
    try:
        messages = [{"role": "user", "content": instruccion}]
        r = requests.post(
            f"{SEO_AGENT_URL}/chat",
            json={"messages": messages},
            timeout=45  # reducido de 60 para no bloquear demasiado el hilo
        )
        r.raise_for_status()
        return {"respuesta": r.json().get("reply", "Sin respuesta")}
    except Exception as e:
        return {"error": str(e)}


TOOL_FNS = {
    "get_products":         get_products,
    "save_content":         save_content,
    "list_content":         list_content,
    "publicar_blog":        publicar_blog,
    "estado_agente_blogs":  estado_agente_blogs,
    "consultar_agente_seo": consultar_agente_seo,
}

def run_tool(name, inputs):
    fn = TOOL_FNS.get(name)
    if not fn:
        return {"error": f"Herramienta no encontrada: {name}"}
    try:
        return fn(**inputs)
    except Exception as e:
        return {"error": str(e)}


# ─── SYSTEM PROMPT (con prompt caching) ──────────────────────────────────────

SYSTEM_PROMPT_TEXT = """Eres un agente experto en creacion de contenido de video para Instagram Reels y TikTok, especializado en la marca Peptidos y Suplementos (peptidosysuplementos.mx).

Tu objetivo: scripts virales, autenticos y que conviertan para el mercado mexicano.

══════════════════════════════════════════════
ESPECIFICACIONES DE PLATAFORMA
══════════════════════════════════════════════

INSTAGRAM REELS:
• Duracion ideal: 7-15s (alcance maximo), 30s, o hasta 90s
• Formato vertical 9:16 (1080x1920px)
• Hook en los primeros 3 segundos — critico para el algoritmo
• Caption: max 2,200 caracteres; solo 125 visibles sin expandir
• Hashtags: 3-5 muy especificos (calidad > cantidad en 2026)
• CTA: "Visita el link en bio", "Comenta X", "Guarda este video"
• Mejor horario MX: L-V 7-9am | 12-2pm | 7-9pm

TIKTOK:
• Duracion ideal: 21-34s (mayor completion rate), hasta 60s para educativo
• Formato vertical 9:16 (1080x1920px)
• Hook CRITICO en el primer segundo — sin intro, empieza en la accion
• Caption: breve (150 chars ideal), max 2,200
• Hashtags: 3-5 mixtos (1 trending, 2 nicho, 1 marca)
• Mejor horario MX: L-V 6-9am | 6-9pm | Fines: 9-11am

══════════════════════════════════════════════
ESTRUCTURA DE UN VIDEO QUE CONVIERTE
══════════════════════════════════════════════

0-3s   HOOK     → Para el scroll. Sin intro, sin "hola soy X"
3-15s  PROBLEMA → Conecta con el dolor o deseo del espectador
15-45s SOLUCIÓN → El producto como respuesta, con datos reales
45-55s PRUEBA   → Resultado, ingrediente clave, respaldo cientifico
55-60s CTA      → Accion especifica y urgente

══════════════════════════════════════════════
HOOKS QUE CONVIERTEN — EJEMPLOS REALES DEL NICHO
══════════════════════════════════════════════

Por curiosidad/intriga:
"¿Por qué yo me recupero en 2 dias y tu en una semana?"
"El peptido que los atletas de elite no mencionen publicamente"
"Esto le pasa a tu cuerpo a los 30 si no haces nada"

Por promesa directa:
"Probé el BPC-157 por 30 dias. Aqui los resultados reales."
"La razon por la que no ves resultados en el gym (no es tu dieta)"
"3 suplementos que uso cada dia para recuperarme como de 20 años"

Por controversia/autoridad:
"Lo que tu nutriologo no te dice sobre la recuperacion muscular"
"Por que la proteina de supermercado es una perdida de dinero"
"El error que comete el 90% de la gente que toma creatina"

Por segmentacion directa:
"Si tienes mas de 30 y entrenas, para y escucha esto"
"Para los que entrenan con lesiones cronicas — esto es para ti"
"Atencion: si llevas mas de 2 años en el gym sin progresar..."

══════════════════════════════════════════════
ÁNGULOS POR TIPO DE PRODUCTO
══════════════════════════════════════════════

BPC-157 / TB-500 (recuperacion):
→ Lesiones cronicas, tendinitis, dolor articular, recuperacion post-entreno
→ Angulo: "la solucion que los medicos no te ofrecen"

Retatrutide / analogos GLP-1 (composicion corporal):
→ Perdida de grasa sin perder musculo, resistencia a la insulina
→ Angulo: "la ciencia detras de la perdida de grasa eficiente"

GHK-Cu / MOTS-c / Longevidad:
→ Biohacking, anti-aging, salud celular, rendimiento cognitivo
→ Angulo: "invierte en tu cuerpo ahora para los proximos 30 anos"

IGF-1 LR3 / HGH (performance):
→ Masa muscular, fuerza, recuperacion, composicion corporal
→ Angulo: "rendimiento de siguiente nivel"

Proteinas / Creatina (basics):
→ Onboarding gym, fundamentos, consistencia, primeros resultados
→ Angulo: "lo que realmente funciona sin complicarse"

══════════════════════════════════════════════
FORMATOS VIRALES 2026
══════════════════════════════════════════════

• POV: "Un dia en mi vida usando X peptido"
• Revelacion: "Lo que nadie te dice sobre [producto]"
• Transformacion: Antes y despues (30/60/90 dias)
• Educativo: "3 cosas que [producto] hace en tu cuerpo en 60s"
• Raw/autentico: camara fija, sin edicion, talking head
• Comparacion: "Antes de X vs despues de X" lado a lado
• Tutorial: "Asi es como yo uso el BPC-157" paso a paso
• Mito vs realidad: "Esto NO es lo que piensas"

══════════════════════════════════════════════
COSAS QUE EVITAR (algoritmo y compliance)
══════════════════════════════════════════════

❌ Mostrar jeringas o agujas en pantalla — ban automatico en IG y TikTok
❌ Palabras: "esteroides", "dopaje", "prohibido", "ilegal" en TikTok
❌ Afirmar que un producto "cura", "trata" o "elimina" enfermedades
❌ Precios en el video — mejor "link en bio" para no parecer spam
❌ Musica con copyright — usar audio trending o sin licencia
❌ Intro larga — los primeros 2 segundos son todo

✅ Usar: "puede ayudar a...", "en mi experiencia...", "se ha estudiado que..."
✅ Siempre: "producto de investigacion" o "suplemento de alto rendimiento"
✅ Testimoniales en primera persona: "yo", "mi cuerpo", "lo que senti"

══════════════════════════════════════════════
CONTEXTO DE NEGOCIO
══════════════════════════════════════════════

• Mercado: Mexico — tono cercano, tutear, no formal
• Quincenas (1 y 15): mejores dias para lanzar promociones en video
• Picos fitness: Enero-Marzo y Agosto-Septiembre
• Hashtags de marca: #PeptidosYSuplementos #MxFitness #BiohackingMX
• Precios siempre en MXN
• Envio gratis en pedidos de MX$7,500 o mas — buen dato para CTA

══════════════════════════════════════════════
AGENTES INTEGRADOS
══════════════════════════════════════════════

🔵 AGENTE SEO → consultar_agente_seo
   Obtiene titulos y keywords reales del catalogo para enriquecer captions

🟣 AGENTE DE BLOGS → publicar_blog + estado_agente_blogs
   Publica un blog complementario al video el mismo dia
   Pipeline: Google Trends → Claude escribe → Unsplash → WordPress

ESTRATEGIA CRUZADA: video + blog el mismo dia = mas SEO + mas alcance

══════════════════════════════════════════════
FLUJO DE TRABAJO
══════════════════════════════════════════════

1. Si el usuario menciona un producto → get_products para datos reales
2. Consulta estado_agente_blogs para saber que temas ya se cubrieron
3. Genera script COMPLETO en el formato de abajo
4. Siempre dos versiones: IG Reels + TikTok
5. Guarda automaticamente con save_content
6. Al final ofrece: variaciones de hook | version en ingles | publicar blog

══════════════════════════════════════════════
FORMATO DE SALIDA (usa EXACTAMENTE esta estructura)
══════════════════════════════════════════════

---
🎬 [NOMBRE DEL VIDEO]
📱 Plataforma: [IG Reels / TikTok]
⏱️ Duracion: [X segundos]
🎯 Objetivo: [awareness / consideracion / conversion]

HOOK (0-3s):
"[texto exacto — entre comillas]"

ESCENAS:
[0-3s]   Visual: ... | Texto en pantalla: ... | Audio: ...
[3-15s]  Visual: ... | Texto en pantalla: ... | Audio: ...
[15-45s] Visual: ... | Texto en pantalla: ... | Audio: ...
[45-55s] Visual: ... | Texto en pantalla: ... | Audio: ...
[55-60s] Visual: ... | Texto en pantalla: ... | Audio: ...

CAPTION:
[texto completo con emojis, max 125 chars visibles primero]

HASHTAGS:
#hashtag1 #hashtag2 #hashtag3 #hashtag4 #hashtag5

AUDIO SUGERIDO:
[tendencia actual o tipo de audio para MX 2026]

CTA:
[llamada a accion especifica y urgente]
---

Responde siempre en espanol salvo que el usuario escriba en ingles."""

# Sistema con cache_control para reducir costo y latencia en llamadas repetidas
SYSTEM = [
    {
        "type": "text",
        "text": SYSTEM_PROMPT_TEXT,
        "cache_control": {"type": "ephemeral"},
    }
]

TOOLS = [
    {
        "name": "get_products",
        "description": "Obtiene el catalogo de Peptidos y Suplementos con nombre, precio, descripcion, categorias y stock. Usalo cuando el usuario mencione un producto especifico.",
        "input_schema": {
            "type": "object",
            "properties": {
                "per_page": {"type": "integer", "description": "Cantidad a obtener (max 100)", "default": 20}
            }
        }
    },
    {
        "name": "save_content",
        "description": "Guarda contenido generado en la biblioteca. Usalo automaticamente despues de generar cualquier script, caption o calendario.",
        "input_schema": {
            "type": "object",
            "required": ["title", "platform", "content_type", "content"],
            "properties": {
                "title": {"type": "string", "description": "Titulo descriptivo, ej: 'Script Reel BPC-157 30s Hook Recuperacion'"},
                "platform": {"type": "string", "description": "instagram | tiktok | ambas"},
                "content_type": {"type": "string", "description": "script | caption | hook | calendar | hashtags | guion"},
                "content": {"type": "string", "description": "El contenido completo generado"},
                "product": {"type": "string", "description": "Nombre del producto asociado (vacio si es general)", "default": ""}
            }
        }
    },
    {
        "name": "list_content",
        "description": "Lista el contenido previamente generado y guardado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "instagram | tiktok | ambas"},
                "content_type": {"type": "string", "description": "script | caption | hook | calendar | hashtags"},
                "limit": {"type": "integer", "default": 10}
            }
        }
    },
    {
        "name": "publicar_blog",
        "description": "Dispara el Agente de Blogs para publicar un articulo en WordPress sobre el mismo tema del video. Ideal para contenido cruzado: video + blog el mismo dia.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Tema especifico (ej: 'BPC-157 para recuperacion muscular'). Omitir para usar tendencias automaticamente."},
                "site_key": {"type": "string", "default": "peptidosysuplementos"}
            }
        }
    },
    {
        "name": "estado_agente_blogs",
        "description": "Consulta el estado del Agente de Blogs: si esta corriendo, ultimo post publicado, errores. Util para alinear temas entre blog y video.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "consultar_agente_seo",
        "description": "Llama al Agente SEO para obtener titulos reales, keywords optimizadas o descripciones del catalogo. Util para enriquecer captions con terminos exactos del producto.",
        "input_schema": {
            "type": "object",
            "required": ["instruccion"],
            "properties": {
                "instruccion": {"type": "string", "description": "Instruccion para el agente SEO. Ej: 'Dame los titulos de los productos de peptidos'"}
            }
        }
    }
]


# ─── RUTAS ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return open("templates/index.html", encoding="utf-8").read()


@app.route("/chat", methods=["POST"])
def chat():
    """
    Endpoint de chat con streaming SSE.
    Fase 1: tool calls sincrónicos (rápidos — fetch de datos).
    Fase 2: respuesta final streamed token a token desde Claude.
    """
    body = request.json
    session_id = body.get("session_id", "default")
    new_messages = body.get("messages", [])

    saved = load_messages(session_id)
    if saved and new_messages:
        messages = saved + [new_messages[-1]]
    elif saved:
        messages = saved
    else:
        messages = new_messages

    def generate():
        nonlocal messages
        try:
            # ── Fase 1: tool call loop (síncrono) ────────────────────────────
            response = client.messages.create(
                model=MODEL,
                max_tokens=8192,
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
                betas=["prompt-caching-2024-07-31"],
            )

            while response.stop_reason == "tool_use":
                # notifica al usuario que se está consultando una herramienta
                tool_names = [b.name for b in response.content if b.type == "tool_use"]
                yield f"data: {json.dumps({'tool': ', '.join(tool_names)})}\n\n"

                ac = []
                for b in response.content:
                    if b.type == "tool_use":
                        ac.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                    elif hasattr(b, "text") and b.text:
                        ac.append({"type": "text", "text": b.text})

                tr = []
                for b in response.content:
                    if b.type == "tool_use":
                        result = run_tool(b.name, b.input)
                        tr.append({
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        })

                messages = messages + [
                    {"role": "assistant", "content": ac},
                    {"role": "user",      "content": tr},
                ]

                response = client.messages.create(
                    model=MODEL,
                    max_tokens=8192,
                    system=SYSTEM,
                    tools=TOOLS,
                    messages=messages,
                    betas=["prompt-caching-2024-07-31"],
                )

            # ── Fase 2: stream la respuesta final token a token ───────────────
            full_reply = ""
            final_messages_for_stream = messages + []

            # Usamos stream() para la llamada final (el texto largo del script)
            with client.messages.stream(
                model=MODEL,
                max_tokens=8192,
                system=SYSTEM,
                messages=final_messages_for_stream,
                # Sin tools en la llamada final para forzar respuesta de texto puro
            ) as stream:
                for text_chunk in stream.text_stream:
                    full_reply += text_chunk
                    yield f"data: {json.dumps({'text': text_chunk})}\n\n"

            # ── Guardar en DB ─────────────────────────────────────────────────
            if full_reply:
                messages.append({"role": "assistant", "content": full_reply})
                save_messages(session_id, messages)

            yield f"data: {json.dumps({'done': True})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/content", methods=["GET"])
def content_library():
    platform = request.args.get("platform")
    content_type = request.args.get("type")
    items = list_content(platform, content_type, limit=30)
    return jsonify({"items": items})


@app.route("/chat/clear", methods=["POST"])
def clear_chat():
    session_id = request.json.get("session_id", "default")
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
    con.commit()
    con.close()
    return jsonify({"success": True})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": MODEL, "agent": "social-video-agent"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
