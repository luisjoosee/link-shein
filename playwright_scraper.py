"""
playwright_scraper.py
Extracción de datos usando un navegador real automatizado (Playwright).

Este método es más lento que scraper.py (requests), pero maneja casos que
requests no puede resolver:
- Links de "compartir" o "onelink.shein.com" que redirigen con JavaScript.
- Páginas donde los datos del producto se cargan después de la carga inicial
  (vía llamadas a la API interna de Shein).

Cómo funciona:
1. Abre la URL en un navegador Chromium headless.
2. Escucha todas las respuestas de red tipo JSON que llegan mientras carga
   la página (ahí suele venir la info real del producto).
3. Espera a que terminen las redirecciones y, si aparece un botón tipo
   "Continuar en el navegador" (común en interstitials de apps), lo hace clic.
4. Extrae los datos primero desde el JSON capturado de la red, y si falta
   algo, hace un segundo intento leyendo el HTML final ya renderizado
   (reutilizando los mismos extractores de scraper.py).

Requisitos (instalar una sola vez):
    pip install playwright
    playwright install chromium
"""

import re
from bs4 import BeautifulSoup

from scraper import (
    ProductResult,
    _from_json_ld,
    _from_meta_tags,
    _from_embedded_json,
    _goods_id_from_url,
)

CONTINUE_BUTTON_TEXTS = [
    "Continuar en el navegador",
    "Continue in browser",
    "Continuar",
    "No, gracias",
    "Ir al sitio web",
    "Continue on web",
    "Use web version",
]


def _search_json_recursive(obj, found=None):
    """Busca recursivamente claves conocidas de producto dentro de un JSON
    capturado del tráfico de red de la página."""
    if found is None:
        found = {}

    if isinstance(obj, dict):
        if obj.get("goods_name") and not found.get("nombre"):
            found["nombre"] = obj["goods_name"]
        if obj.get("goods_sn") and not found.get("serial"):
            found["serial"] = str(obj["goods_sn"])
        if obj.get("goods_img") and not found.get("foto_url"):
            img = obj["goods_img"]
            found["foto_url"] = ("https:" + img) if img.startswith("//") else img

        for price_key in ("salePrice", "retailPrice"):
            val = obj.get(price_key)
            if isinstance(val, dict) and not found.get("precio"):
                amt = val.get("amountWithSymbol") or val.get("amount")
                if amt:
                    found["precio"] = str(amt)

        for v in obj.values():
            if isinstance(v, (dict, list)):
                _search_json_recursive(v, found)

    elif isinstance(obj, list):
        tallas = [
            item.get("attr_value_name")
            for item in obj
            if isinstance(item, dict) and item.get("attr_value_name")
        ]
        if tallas and "tallas" not in found:
            found["tallas"] = ", ".join(sorted(set(tallas))[:15])
        for item in obj:
            if isinstance(item, (dict, list)):
                _search_json_recursive(item, found)

    return found


def fetch_product_playwright(url: str, timeout_ms: int = 25000) -> ProductResult:
    from playwright.sync_api import sync_playwright  # import diferido

    result = ProductResult(link=url)
    captured_jsons = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="es-ES",
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()

            def handle_response(response):
                try:
                    ctype = response.headers.get("content-type", "")
                    if "application/json" in ctype:
                        captured_jsons.append(response.json())
                except Exception:
                    pass

            page.on("response", handle_response)

            try:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except Exception:
                # A veces networkidle nunca llega por trackers en segundo plano;
                # seguimos igual con lo que haya cargado.
                pass

            page.wait_for_timeout(1500)

            for text in CONTINUE_BUTTON_TEXTS:
                try:
                    locator = page.get_by_text(text, exact=False)
                    if locator.count() > 0:
                        locator.first.click(timeout=2000)
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    continue

            final_url = page.url
            html = page.content()
            browser.close()

    except Exception as exc:
        result.estado = "error"
        result.detalle_error = f"Error con navegador automatizado: {exc}"
        return result

    merged = {}
    for data in captured_jsons:
        for k, v in _search_json_recursive(data).items():
            merged.setdefault(k, v)

    soup = BeautifulSoup(html, "html.parser")
    for k, v in _from_meta_tags(soup).items():
        merged.setdefault(k, v)
    for k, v in _from_json_ld(soup).items():
        merged.setdefault(k, v)
    for k, v in _from_embedded_json(html).items():
        merged.setdefault(k, v)

    if not merged.get("nombre") and soup.title:
        merged["nombre"] = soup.title.get_text(strip=True)
    if not merged.get("serial"):
        merged["serial"] = _goods_id_from_url(final_url) or _goods_id_from_url(url)

    result.nombre = merged.get("nombre", "")
    result.precio = merged.get("precio", "")
    result.tallas = merged.get("tallas", "")
    result.serial = merged.get("serial", "")
    result.foto_url = merged.get("foto_url", "")

    if not any([result.nombre, result.precio, result.foto_url]):
        result.estado = "sin_datos"
        result.detalle_error = (
            f"No se encontraron datos incluso con navegador automatizado. "
            f"URL final resuelta: {final_url}. Es posible que el link requiera "
            f"iniciar sesión o que el contenido sea exclusivo de la app."
        )

    return result


def batch_scrape_playwright(urls: list[str]) -> list[ProductResult]:
    results = []
    for url in urls:
        url = url.strip()
        if url:
            results.append(fetch_product_playwright(url))
    return results
