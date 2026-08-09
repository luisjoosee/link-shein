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

import os
from urllib.parse import urlparse

from scraper import (
    ProductResult,
    REGIONS,
    CORE_FIELDS,
    _is_complete,
    _merge_results,
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


# JavaScript que corre DENTRO de la página ya cargada para leer directamente
# lo que se ve en pantalla: el precio mostrado, y la talla/color que están
# marcados como seleccionados (los que corresponden exactamente a como se
# abrió el link). Es más confiable que adivinar por el JSON interno, porque
# lee lo mismo que vería una persona.
_DOM_EXTRACT_JS = r"""
() => {
  function textOf(el) {
    return (el && (el.innerText || el.textContent) || "").trim();
  }

  function looksLikePrice(text) {
    return /[$€£]\s?\d/.test(text) || /\d+[.,]\d{2}/.test(text);
  }

  function findPrice() {
    const candidates = Array.from(
      document.querySelectorAll('[class*="price" i], [class*="Price"]')
    );
    for (const el of candidates) {
      const text = textOf(el);
      if (text && looksLikePrice(text) && text.length < 40) {
        return text;
      }
    }
    return "";
  }

  function findSelectedAttribute(keywords) {
    const all = Array.from(document.querySelectorAll("*"));
    for (const el of all) {
      const cls = typeof el.className === "string" ? el.className.toLowerCase() : "";
      const ariaSelected = el.getAttribute && el.getAttribute("aria-selected") === "true";
      const ariaChecked = el.getAttribute && el.getAttribute("aria-checked") === "true";
      const looksSelected =
        /selected|active|checked|current/.test(cls) || ariaSelected || ariaChecked;
      if (!looksSelected) continue;

      let node = el;
      for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
        const nodeCls = typeof node.className === "string" ? node.className.toLowerCase() : "";
        let attrsText = "";
        if (node.attributes) {
          for (const a of node.attributes) {
            attrsText += " " + a.name.toLowerCase() + "=" + String(a.value).toLowerCase();
          }
        }
        const haystack = nodeCls + attrsText;
        if (keywords.some((k) => haystack.includes(k))) {
          const text = textOf(el);
          if (text && text.length < 40) return text;
        }
      }
    }
    return "";
  }

  return {
    precio_dom: findPrice(),
    talla_dom: findSelectedAttribute(["size", "talla"]),
    color_dom: findSelectedAttribute(["color", "colour", "colo"]),
  };
}
"""


def _extract_from_page(context, url: str, timeout_ms: int) -> ProductResult:
    """Abre una pestaña nueva en un contexto de navegador YA ABIERTO y extrae
    los datos del producto. No abre ni cierra el navegador (eso lo hace quien
    llama a esta función), lo cual es clave para procesar varios links rápido:
    abrir un navegador nuevo por cada link es lo más lento del proceso."""
    result = ProductResult(link=url)
    captured_jsons = []
    page = context.new_page()

    def handle_response(response):
        try:
            ctype = response.headers.get("content-type", "")
            if "application/json" in ctype:
                captured_jsons.append(response.json())
        except Exception:
            pass

    page.on("response", handle_response)

    dom_fields = {}
    try:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # Esperamos un poco a que terminen llamadas AJAX típicas, sin
            # bloquear tanto como "networkidle" (que a veces nunca se cumple
            # por trackers/anuncios en segundo plano).
            page.wait_for_timeout(1800)
        except Exception:
            pass

        for text in CONTINUE_BUTTON_TEXTS:
            try:
                locator = page.get_by_text(text, exact=False)
                if locator.count() > 0:
                    locator.first.click(timeout=1500)
                    page.wait_for_timeout(1200)
                    break
            except Exception:
                continue

        # Leemos directamente lo que se ve en pantalla (precio, talla y color
        # ya seleccionados). Esto se hace ANTES de leer el HTML "congelado".
        try:
            dom_fields = page.evaluate(_DOM_EXTRACT_JS) or {}
        except Exception:
            dom_fields = {}

        final_url = page.url
        html = page.content()
    except Exception as exc:
        result.estado = "error"
        result.detalle_error = f"Error con navegador automatizado: {exc}"
        page.close()
        return result

    page.close()

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

    # Lo leído directamente en pantalla (DOM) tiene prioridad, porque refleja
    # exactamente lo seleccionado en el link, no una lista genérica de opciones.
    if dom_fields.get("precio_dom"):
        result.precio = dom_fields["precio_dom"]
    if dom_fields.get("talla_dom"):
        result.tallas = dom_fields["talla_dom"]
    if dom_fields.get("color_dom"):
        result.color = dom_fields["color_dom"]

    if not any([result.nombre, result.precio, result.foto_url]):
        result.estado = "sin_datos"
        result.detalle_error = (
            f"No se encontraron datos incluso con navegador automatizado. "
            f"URL final resuelta: {final_url}. Es posible que el link requiera "
            f"iniciar sesión o que el contenido sea exclusivo de la app."
        )

    return result


def _root_domain(url: str) -> str:
    host = urlparse(url).hostname or ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _new_context(browser, region: dict = None, url: str = ""):
    """Crea un contexto nuevo (pestaña aislada con sus propias cookies) en un
    navegador YA ABIERTO. Si se pasa `region`, aplica headers/cookies/proxy
    para simular esa ubicación."""
    proxy = None
    if region:
        proxy_url = os.environ.get(region["proxy_env"])
        if proxy_url:
            proxy = {"server": proxy_url}

    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US" if region and region["code"] == "US" else "es-ES",
        viewport={"width": 1280, "height": 900},
        extra_http_headers={"Accept-Language": region["accept_language"]} if region else {},
        proxy=proxy,
    )

    if region and url:
        domain = _root_domain(url)
        try:
            context.add_cookies(
                [
                    {"name": k, "value": v, "domain": domain, "path": "/"}
                    for k, v in region["cookies"].items()
                ]
            )
        except Exception:
            pass  # si el dominio no es válido para cookies, seguimos sin ellas

    return context


def diagnostico_pagina(url: str, timeout_ms: int = 25000) -> dict:
    """Abre el link con el navegador automatizado y devuelve el HTML final
    completo + una captura de pantalla, SIN intentar extraer nada. Sirve para
    diagnosticar por qué un link no trae precio/talla/color: se descarga el
    HTML real de la página para poder revisar cómo está armada exactamente
    (nombres de clase, estructura JSON, etc.) y ajustar el scraper con datos
    reales en vez de adivinar."""
    from playwright.sync_api import sync_playwright

    info = {"ok": False, "html": "", "screenshot": b"", "final_url": url, "error": ""}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = _new_context(browser, region=REGIONS[0], url=url)
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(2500)
                for text in CONTINUE_BUTTON_TEXTS:
                    try:
                        locator = page.get_by_text(text, exact=False)
                        if locator.count() > 0:
                            locator.first.click(timeout=1500)
                            page.wait_for_timeout(1200)
                            break
                    except Exception:
                        continue
                info["final_url"] = page.url
                info["html"] = page.content()
                info["screenshot"] = page.screenshot(full_page=True)
                info["ok"] = True
            except Exception as exc:
                info["error"] = str(exc)
            finally:
                page.close()
            browser.close()
    except Exception as exc:
        info["error"] = str(exc)

    return info


def fetch_product_playwright(url: str, timeout_ms: int = 25000) -> ProductResult:
    """Extrae un producto probando distintas regiones (EE.UU., Venezuela) con
    el navegador automatizado, combinando los campos que cada intento logre
    traer. Para varios links de una vez, usa batch_scrape_playwright."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            intentos = []
            for region in REGIONS:
                context = _new_context(browser, region=region, url=url)
                r = _extract_from_page(context, url, timeout_ms)
                r._region_code = region["code"]
                context.close()
                intentos.append(r)
                if _is_complete(r):
                    break
            browser.close()
            return _merge_results(intentos)
    except Exception as exc:
        return ProductResult(
            link=url, estado="error", detalle_error=f"Error con navegador automatizado: {exc}"
        )


def batch_scrape_playwright(
    urls: list[str], timeout_ms: int = 25000, progress_callback=None
) -> list[ProductResult]:
    """Procesa varios links reutilizando UN SOLO navegador (mucho más rápido
    que abrir uno nuevo por cada link). Para cada link prueba las regiones
    definidas en scraper.REGIONS y combina los campos obtenidos.
    progress_callback(hecho, total) es opcional, para la barra de progreso."""
    from playwright.sync_api import sync_playwright

    clean_urls = [u.strip() for u in urls if u.strip()]
    results = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            for i, url in enumerate(clean_urls):
                intentos = []
                for region in REGIONS:
                    context = _new_context(browser, region=region, url=url)
                    r = _extract_from_page(context, url, timeout_ms)
                    r._region_code = region["code"]
                    context.close()
                    intentos.append(r)
                    if _is_complete(r):
                        break
                results.append(_merge_results(intentos))
                if progress_callback:
                    progress_callback(i + 1, len(clean_urls))
            browser.close()
    except Exception as exc:
        for url in clean_urls[len(results):]:
            results.append(
                ProductResult(link=url, estado="error", detalle_error=str(exc))
            )

    return results
