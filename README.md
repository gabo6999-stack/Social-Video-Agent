# Social Video Agent (multisitio)

Agente con Claude API que genera guiones de video para Instagram Reels y
TikTok — hook, escenas con timing, caption, hashtags y CTA — con datos reales
del catálogo cuando el sitio lo tiene, biblioteca persistente en PostgreSQL, y
conexión a agentes hermanos (Blog, SEO) cuando aplica.

## Cómo funciona

1. Cada marca vive como un archivo de configuración en `sites/*.json`: nicho,
   audiencia, tono, objetivo, contexto de negocio, ángulos de producto, hooks
   de ejemplo, compliance del rubro, y qué herramientas tiene disponibles
   (`get_products` solo si `woocommerce: true`, agentes hermanos solo si
   están listados en `tools`).
2. `video_framework.py` define el framework de guion común a todos los sitios
   (specs de plataforma, estructura del video, secuencia de arranque en frío
   para tandas de contenido, ciclo de medición) — no cambia por sitio.
3. `main.py` combina framework + perfil del sitio activo en el system prompt,
   arma la lista de herramientas permitidas para ese sitio, y sirve todo vía
   Flask con streaming (SSE).
4. El navegador pregunta primero **sitio y red(es) social(es)** antes de
   dejar chatear (`/api/start`), y ese contexto queda fijo el resto de la
   sesión — no se vuelve a preguntar nicho, tono ni plataforma.

## Setup local

```bash
pip install -r requirements.txt
cp .env.txt .env   # y llena los valores reales
python main.py
```

Requiere una instancia de PostgreSQL accesible vía `DATABASE_URL`. En
Railway se inyecta sola.

## Agregar una marca nueva

Crea `sites/<nombre>.json` con este esquema (ver `sites/arcademotors.json`
para un ejemplo sin WooCommerce ni agentes hermanos, o
`sites/peptidosysuplementos.json` para uno completo):

```json
{
  "key": "nombre",
  "brand": "Nombre de la marca",
  "site_url": "sitio.com",
  "niche": "A qué se dedica",
  "audience": "A quién le habla",
  "platforms": ["Instagram", "TikTok"],
  "tone": "...",
  "goal": "Qué acción de negocio busca el video",
  "business_context": "Estacionalidad, mercado, datos útiles para CTA",
  "brand_hashtags": ["#Marca"],
  "product_angles": [{"product": "...", "focus": "...", "angle": "..."}],
  "hooks_examples": ["..."],
  "viral_formats": ["..."],
  "compliance": ["Reglas específicas del rubro/plataforma"],
  "tools": ["get_products", "publicar_blog", "estado_agente_blogs", "consultar_agente_seo"],
  "woocommerce": false,
  "sibling_agents": [{"name": "...", "url": "...", "icon": "🔵", "desc": "..."}],
  "quick_prompts": [{"icon": "🎬", "label": "...", "prompt": "..."}],
  "cross_prompts": []
}
```

No requiere cambios en `main.py` ni en la plantilla. `tools` controla
exactamente qué herramientas ve el modelo para ese sitio — un sitio sin
WooCommerce nunca debe listar `get_products`.

## Variables de entorno

Ver `.env.txt` (cópialo como `.env`). `WC_*` solo aplica a sitios con
`woocommerce: true`. `BLOG_AGENT_URL`/`SEO_AGENT_URL` solo aplican a sitios
que listan esas tools. `NEXUS_URL`/`NEXUS_KEY` son opcionales.

## Notas de migración

- `content_library` tiene una columna `site_key` (migración aditiva vía
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, corre sola en `init_db()`) para
  filtrar la biblioteca por sitio sin perder contenido histórico.
- Contenido guardado antes de esta migración queda con `site_key = NULL` y no
  aparecerá en la biblioteca de ningún sitio hasta reasignarlo manualmente.
