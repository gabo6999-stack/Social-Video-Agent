from dotenv import load_dotenv

load_dotenv()  # debe ser lo primero antes de cualquier os.environ.get()

import os
import json
import psycopg2
import psycopg2.extras
import requests
from datetime import datetime
from flask import Flask, request, jsonify, Response, stream_with_context, render_template
from requests.auth import HTTPBasicAuth
import anthropic

from video_framework import VIDEO_FRAMEWORK

# ─── CONFIG ──────────────────────────────────────────────────────────────────────────────────

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
SITES_DIR = os.path.join(BASE_DIR, "sites")

BLOG_AGENT_URL = os.environ.get("BLOG_AGENT_URL", "https://agente-blogs-production.up.railway.app")
SEO_AGENT_URL  = os.environ.get("SEO_AGENT_URL",  "https://web-production-3743c.up.railway.app")
WC_URL         = os.environ.get("WC_STORE_URL", "").rstrip("/")
WC_KEY         = os.environ.get("WC_CONSUMER_KEY", "")
WC_SECRET      = os.environ.get("WC_CONSUMER_SECRET", "")
DATABASE_URL   = os.environ.get("DATABASE_URL", "")   # Railway inyecta esto automaticamente
MODEL          = "claude-sonnet-4-6"

app    = Flask(__name__)
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


def notify_nexus(action, detail=None, url=None):
    """Reporta una actividad a NEXUS (Centro de Comando). Solo corre si hay NEXUS_URL y NEXUS_KEY."""
    nexus_url = os.environ.get("NEXUS_URL")
    nexus_key = os.environ.get("NEXUS_KEY")
    if not nexus_url or not nexus_key:
        return
    try:
        requests.post(
            f"{nexus_url}/api/ingest",
            json={"agent": "Social Media Agent", "action": action, "detail": detail, "url": url},
            headers={"x-nexus-key": nexus_key},
            timeout=15,
        )
    except Exception as e:
        print(f"[NEXUS] No se pudo reportar: {e}")


# ─── Sitios (multisitio) ───────────────────────────────────────────────

def list_sites():
    if not os.path.isdir(SITES_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(SITES_DIR) if f.endswith(".json"))


def load_site(site_key):
    path = os.path.join(SITES_DIR, f"{site_key}.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_all_sites():
    return [dict(load_site(k), key=k) for k in list_sites()]


def build_system_prompt(site, platforms):
    lines = [VIDEO_FRAMEWORK, "\n\n---\n\nPERFIL DEL SITIO ACTIVO:\n"]
    lines.append(f"MARCA: {site['brand']} ({site.get('site_url', '')})")
    lines.append(f"NICHO: {site['niche']}")
    lines.append(f"AUDIENCIA: {site['audience']}")
    lines.append(f"PLATAFORMA(S) DE ESTA SESIÓN: {', '.join(platforms)}")
    lines.append(f"TONO: {site['tone']}")
    lines.append(f"OBJETIVO: {site['goal']}")
    lines.append(f"CONTEXTO DE NEGOCIO: {site.get('business_context', '-')}")
    lines.append(f"HASHTAGS DE MARCA: {' '.join(site.get('brand_hashtags', []))}")

    if site.get("product_angles"):
        lines.append("\nÁNGULOS POR TEMA/PRODUCTO:")
        for a in site["product_angles"]:
            lines.append(f"- {a['product']}: {a['focus']} → ángulo: \"{a['angle']}\"")

    if site.get("hooks_examples"):
        lines.append("\nEJEMPLOS DE HOOKS QUE FUNCIONAN EN ESTE NICHO:")
        for h in site["hooks_examples"]:
            lines.append(f"- \"{h}\"")

    if site.get("viral_formats"):
        lines.append("\nFORMATOS QUE FUNCIONAN PARA ESTA MARCA:")
        for vf in site["viral_formats"]:
            lines.append(f"- {vf}")

    if site.get("compliance"):
        lines.append("\nCOMPLIANCE OBLIGATORIO PARA ESTE SITIO:")
        for c in site["compliance"]:
            lines.append(f"- {c}")

    if site.get("sibling_agents"):
        lines.append("\nAGENTES HERMANOS DISPONIBLES:")
        for sa in site["sibling_agents"]:
            lines.append(f"- {sa['name']}: {sa['desc']}")

    text = "\n".join(lines) + f"\n\nHoy es {datetime.now().strftime('%Y-%m-%d')}."
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# ─── BASE DE DATOS (PostgreSQL) ──────────────────────────────────────────

def get_conn():
    """Retorna una conexion a PostgreSQL usando DATABASE_URL de Railway."""
    return psycopg2.connect(DATABASE_URL)

def init_db():
    con = get_conn()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id         SERIAL PRIMARY KEY,
            session_id TEXT,
            role       TEXT,
            content    TEXT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS content_library (
            id           SERIAL PRIMARY KEY,
            title        TEXT,
            platform     TEXT,
            content_type TEXT,
            content      TEXT,
            product      TEXT,
            created_at   TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_sessions (
            session_id TEXT PRIMARY KEY,
            site_key   TEXT,
            platforms  TEXT,
            updated_at TEXT
        )
    """)
    # Migracion aditiva: sitios se agregaron despues de que estas tablas ya
    # existian en produccion — content_library.site_key permite filtrar la
    # biblioteca por sitio sin perder el contenido ya guardado.
    cur.execute("ALTER TABLE content_library ADD COLUMN IF NOT EXISTS site_key TEXT")
    con.commit()
    cur.close()
    con.close()

init_db()

def save_messages(session_id, messages):
    con = get_conn()
    cur = con.cursor()
    cur.execute("DELETE FROM conversations WHERE session_id = %s", (session_id,))
    for m in messages:
        content = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])
        cur.execute(
            "INSERT INTO conversations (session_id, role, content, created_at) VALUES (%s, %s, %s, %s)",
            (session_id, m["role"], content, datetime.now().isoformat())
        )
    con.commit()
    cur.close()
    con.close()

def load_messages(session_id):
    con = get_conn()
    cur = con.cursor()
    cur.execute(
        "SELECT role, content FROM conversations WHERE session_id = %s ORDER BY id",
        (session_id,)
    )
    rows = cur.fetchall()
    cur.close()
    con.close()
    messages = []
    for role, content in rows:
        try:
            parsed = json.loads(content)
            messages.append({"role": role, "content": parsed})
        except Exception:
            messages.append({"role": role, "content": content})
    return messages


def save_session_site(session_id, site_key, platforms):
    con = get_conn()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO agent_sessions (session_id, site_key, platforms, updated_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (session_id) DO UPDATE
        SET site_key = EXCLUDED.site_key, platforms = EXCLUDED.platforms, updated_at = EXCLUDED.updated_at
        """,
        (session_id, site_key, json.dumps(platforms), datetime.now().isoformat())
    )
    con.commit()
    cur.close()
    con.close()


def load_session_site(session_id):
    con = get_conn()
    cur = con.cursor()
    cur.execute("SELECT site_key, platforms FROM agent_sessions WHERE session_id = %s", (session_id,))
    row = cur.fetchone()
    cur.close()
    con.close()
    if not row:
        return None, None
    site_key, platforms = row
    try:
        platforms = json.loads(platforms)
    except Exception:
        platforms = []
    return site_key, platforms

# ─── HERRAMIENTAS ──────────────────────────────────────────────────────

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

def save_content(site_key, title, platform, content_type, content, product=""):
    try:
        con = get_conn()
        cur = con.cursor()
        cur.execute(
            "INSERT INTO content_library (title, platform, content_type, content, product, created_at, site_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (title, platform, content_type, content, product, datetime.now().isoformat(), site_key)
        )
        item_id = cur.fetchone()[0]
        con.commit()
        cur.close()
        con.close()
        notify_nexus(
            action="Genero un script de video",
            detail=f"{content_type} — {title} ({platform}) [{site_key}]",
        )
        return {"success": True, "id": item_id, "saved": title}
    except Exception as e:
        return {"error": str(e)}

def list_content(site_key, platform=None, content_type=None, limit=10):
    try:
        con = get_conn()
        cur = con.cursor()
        query  = "SELECT id, title, platform, content_type, product, created_at FROM content_library WHERE site_key = %s"
        params = [site_key]
        if platform:
            query += " AND platform = %s"
            params.append(platform)
        if content_type:
            query += " AND content_type = %s"
            params.append(content_type)
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(int(limit))
        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
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

# ─── INTEGRACION CON AGENTES HERMANOS ──────────────────────────────────────

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
            timeout=45
        )
        r.raise_for_status()
        return {"respuesta": r.json().get("reply", "Sin respuesta")}
    except Exception as e:
        return {"error": str(e)}

TOOL_FNS = {
    "get_products":        get_products,
    "save_content":        save_content,
    "list_content":        list_content,
    "publicar_blog":       publicar_blog,
    "estado_agente_blogs": estado_agente_blogs,
    "consultar_agente_seo": consultar_agente_seo,
}

# Tools que siempre estan disponibles + su definicion. Las opcionales
# (get_products, publicar_blog, estado_agente_blogs, consultar_agente_seo) se
# agregan por sitio segun site["tools"] — asi un sitio sin WooCommerce ni
# agentes hermanos (ej. arcademotors) nunca las ve ofrecidas por el modelo.
BASE_TOOL_DEFS = {
    "save_content": {
        "name": "save_content",
        "description": "Guarda contenido generado en la biblioteca de este sitio. Usalo automaticamente despues de generar cualquier script, caption o calendario.",
        "input_schema": {
            "type": "object",
            "required": ["title", "platform", "content_type", "content"],
            "properties": {
                "title":        {"type": "string", "description": "Titulo descriptivo, ej: 'Script Reel BPC-157 30s Hook Recuperacion'"},
                "platform":     {"type": "string", "description": "instagram | tiktok | ambas"},
                "content_type": {"type": "string", "description": "script | caption | hook | calendar | hashtags | guion"},
                "content":      {"type": "string", "description": "El contenido completo generado"},
                "product":      {"type": "string", "description": "Nombre del producto/tema asociado (vacio si es general)", "default": ""}
            }
        }
    },
    "list_content": {
        "name": "list_content",
        "description": "Lista el contenido previamente generado y guardado para este sitio.",
        "input_schema": {
            "type": "object",
            "properties": {
                "platform":     {"type": "string", "description": "instagram | tiktok | ambas"},
                "content_type": {"type": "string", "description": "script | caption | hook | calendar | hashtags"},
                "limit":        {"type": "integer", "default": 10}
            }
        }
    },
}

OPTIONAL_TOOL_DEFS = {
    "get_products": {
        "name": "get_products",
        "description": "Obtiene el catalogo real de productos con nombre, precio, descripcion, categorias y stock. Usalo cuando el usuario mencione un producto especifico.",
        "input_schema": {
            "type": "object",
            "properties": {
                "per_page": {"type": "integer", "description": "Cantidad a obtener (max 100)", "default": 20}
            }
        }
    },
    "publicar_blog": {
        "name": "publicar_blog",
        "description": "Dispara el Agente de Blogs para publicar un articulo en WordPress sobre el mismo tema del video. Ideal para contenido cruzado: video + blog el mismo dia.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic":    {"type": "string", "description": "Tema especifico. Omitir para usar tendencias automaticamente."},
                "site_key": {"type": "string", "default": "peptidosysuplementos"}
            }
        }
    },
    "estado_agente_blogs": {
        "name": "estado_agente_blogs",
        "description": "Consulta el estado del Agente de Blogs: si esta corriendo, ultimo post publicado, errores. Util para alinear temas entre blog y video.",
        "input_schema": {"type": "object", "properties": {}}
    },
    "consultar_agente_seo": {
        "name": "consultar_agente_seo",
        "description": "Llama al Agente SEO para obtener titulos reales, keywords optimizadas o descripciones del catalogo.",
        "input_schema": {
            "type": "object",
            "required": ["instruccion"],
            "properties": {
                "instruccion": {"type": "string", "description": "Instruccion para el agente SEO."}
            }
        }
    },
}


def build_tools(site):
    tools = list(BASE_TOOL_DEFS.values())
    for name in site.get("tools", []):
        if name in OPTIONAL_TOOL_DEFS:
            tools.append(OPTIONAL_TOOL_DEFS[name])
    return tools


def run_tool(name, inputs, site_key):
    fn = TOOL_FNS.get(name)
    if not fn:
        return {"error": f"Herramienta no encontrada: {name}"}
    try:
        if name in ("save_content", "list_content"):
            return fn(site_key=site_key, **inputs)
        return fn(**inputs)
    except Exception as e:
        return {"error": str(e)}


# ─── RUTAS ──────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", sites=load_all_sites())

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True) or {}
    session_id = data.get("session_id")
    site_key = data.get("site")
    platforms = [p for p in (data.get("platforms") or []) if p]

    if not session_id:
        return jsonify({"error": "Falta session_id."}), 400
    site = load_site(site_key)
    if not site:
        return jsonify({"error": "Sitio invalido."}), 400
    if not platforms:
        return jsonify({"error": "Selecciona al menos una red social."}), 400

    save_session_site(session_id, site_key, platforms)
    return jsonify({"brand": site["brand"], "platforms": platforms})

@app.route("/chat", methods=["POST"])
def chat():
    body       = request.json
    session_id = body.get("session_id", "default")
    new_messages = body.get("messages", [])

    site_key, platforms = load_session_site(session_id)
    if not site_key:
        return jsonify({"error": "Sesion sin sitio asignado. Llama a /api/start primero."}), 400
    site = load_site(site_key)
    if not site:
        return jsonify({"error": f"Sitio '{site_key}' ya no existe."}), 400

    system = build_system_prompt(site, platforms)
    tools = build_tools(site)

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
            # ── Fase 1: tool call loop (sincrono) ────────────────────────
            response = client.messages.create(
                model=MODEL,
                max_tokens=8192,
                system=system,
                tools=tools,
                messages=messages,
            )

            while response.stop_reason == "tool_use":
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
                        result = run_tool(b.name, b.input, site_key)
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
                    system=system,
                    tools=tools,
                    messages=messages,
                )

            # ── Fase 2: extraer texto de la respuesta final de Fase 1 ────
            full_reply = ""
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    full_reply += block.text
                    yield f"data: {json.dumps({'text': block.text})}\n\n"

            # ── Guardar en DB ────────────────────────────────────
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
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":       "keep-alive",
        },
    )

@app.route("/content", methods=["GET"])
def content_library():
    site_key = request.args.get("site")
    if not site_key:
        return jsonify({"error": "Falta parametro site."}), 400
    platform     = request.args.get("platform")
    content_type = request.args.get("type")
    items = list_content(site_key, platform, content_type, limit=30)
    return jsonify({"items": items})

@app.route("/chat/clear", methods=["POST"])
def clear_chat():
    session_id = request.json.get("session_id", "default")
    con = get_conn()
    cur = con.cursor()
    cur.execute("DELETE FROM conversations WHERE session_id = %s", (session_id,))
    con.commit()
    cur.close()
    con.close()
    return jsonify({"success": True})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "model": MODEL, "agent": "social-media-agent", "sites": list_sites()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
