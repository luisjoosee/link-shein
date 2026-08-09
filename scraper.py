"""
scraper.py
Lógica para extraer nombre, precio, tallas, SKU/serial y foto
a partir de un link de producto de Shein.

Estrategia (en orden de prioridad):
1. Buscar bloque JSON-LD (<script type="application/ld+json">) con schema.org Product.
2. Buscar meta tags Open Graph (og:title, og:image, product:price:amount).
3. Buscar JSON "crudo" embebido en el HTML (los sitios tipo Shein suelen incrustar
   los datos del producto en un <script> como variable JS, ej: window.gbRawData = {...}).
   Se usa regex tolerante a errores para sacar claves comunes: goods_sn, goods_img,
   goods_name, salePrice/retailPrice, size.
4. Como último recurso, se usa el <title> de la página como nombre.

Nota: Shein puede bloquear peticiones automatizadas (Cloudflare / JS challenge).
Si un link devuelve datos vacíos, probablemente el sitio bloqueó la petición;
revisa el README para la alternativa con navegador (Playwright).
"""

import re
import json
import os
import random
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
]

# Intentos de "región" para cuando el catálogo/precio varía según el país.
# Nota: Shein decide la ubicación principalmente por la IP de la conexión.
# Los headers/cookies de abajo son un intento razonable (mejor que nada),
# pero si no fuera suficiente, se puede definir un proxy real de ese país
# en las variables de entorno SHEIN_PROXY_US / SHEIN_PROXY_VE (ver README).
REGIONS = [
    {
        "code": "US",
        "accept_language": "en-US,en;q=0.9",
        "cookies": {"country": "US", "currency": "USD", "language": "en"},
        "proxy_env": "SHEIN_PROXY_US",
    },
    {
        "code": "VE",
        "accept_language": "es-VE,es;q=0.9,es-419;q=0.8",
        "cookies": {"country": "VE", "currency": "USD", "language": "es"},
        "proxy_env": "SHEIN_PROXY_VE",
    },
]

# Campos que consideramos "esenciales" para dar el producto por completo.
# "tallas" queda fuera porque muchos productos (accesorios, etc.) no tienen.
CORE_FIELDS = ["nombre", "precio", "serial", "foto_url"]


@dataclass
class ProductResult:
    link: str
    nombre: str = ""
    precio: str = ""
    tallas: str = ""
    color: str = ""
    serial: str = ""
    foto_url: str = ""
    estado: str = "ok"
    detalle_error: str = ""


def _is_complete(result: "ProductResult") -> bool:
    """True si ya tenemos todos los campos esenciales (no hace falta seguir
    intentando con otra región/método)."""
    return all(getattr(result, f) for f in CORE_FIELDS)


def merge_into(base: "ProductResult", extra: "ProductResult") -> "ProductResult":
    """Completa los campos vacíos de `base` usando los valores de `extra`,
    sin pisar los que `base` ya tenía. Útil para combinar el resultado del
    modo rápido con un reintento (otra región, o navegador automatizado)."""
    for f in CORE_FIELDS + ["tallas", "color"]:
        if not getattr(base, f) and getattr(extra, f):
            setattr(base, f, getattr(extra, f))

    if _is_complete(base):
        base.estado = "ok"
        base.detalle_error = ""
    elif any(getattr(base, f) for f in CORE_FIELDS):
        faltantes = [f for f in CORE_FIELDS if not getattr(base, f)]
        base.estado = "incompleto"
        base.detalle_error = f"Faltan campos: {', '.join(faltantes)}."
    return base


def _merge_results(results: list) -> "ProductResult":
    """Combina varios intentos (por ejemplo, uno por región) en un solo
    resultado, quedándose con el primer valor no vacío de cada campo."""
    merged = ProductResult(link=results[0].link)
    for r in results:
        merge_into(merged, r)

    notas = [r.detalle_error for r in results if r.detalle_error]
    if not _is_complete(merged):
        faltantes = [f for f in CORE_FIELDS if not getattr(merged, f)]
        intentos_desc = ", ".join(
            r_region for r_region in [getattr(r, "_region_code", None) for r in results] if r_region
        )
        merged.detalle_error = (
            f"Faltan campos: {', '.join(faltantes)} (se probó: {intentos_desc or 'región por defecto'})."
        )
        if any(getattr(merged, f) for f in CORE_FIELDS):
            merged.estado = "incompleto"
        else:
            merged.estado = "sin_datos"
    else:
        merged.estado = "ok"
    return merged


def _headers(region: dict = None):
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Referer": "https://www.google.com/",
    }
    if region:
        headers["Accept-Language"] = region["accept_language"]
    return headers


def _goods_id_from_url(url: str) -> str:
    """Intenta sacar el ID del producto de la URL como fallback de serial."""
    m = re.search(r"-p-(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"goods_id=(\d+)", url)
    if m:
        return m.group(1)
    return ""


def _from_json_ld(soup: BeautifulSoup) -> dict:
    data = {}
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "Product" or "offers" in item:
                if item.get("name"):
                    data["nombre"] = item["name"]
                if item.get("sku"):
                    data["serial"] = str(item["sku"])
                image = item.get("image")
                if isinstance(image, list) and image:
                    data["foto_url"] = image[0]
                elif isinstance(image, str):
                    data["foto_url"] = image
                offers = item.get("offers")
                if isinstance(offers, dict):
                    price = offers.get("price")
                    currency = offers.get("priceCurrency", "")
                    if price:
                        data["precio"] = f"{price} {currency}".strip()
                elif isinstance(offers, list) and offers:
                    price = offers[0].get("price")
                    currency = offers[0].get("priceCurrency", "")
                    if price:
                        data["precio"] = f"{price} {currency}".strip()
    return data


def _from_meta_tags(soup: BeautifulSoup) -> dict:
    data = {}
    mapping = {
        "og:title": "nombre",
        "og:image": "foto_url",
        "product:price:amount": "precio",
    }
    for prop, key in mapping.items():
        tag = soup.find("meta", {"property": prop})
        if tag and tag.get("content"):
            data[key] = tag["content"]
    return data


def _from_embedded_json(html: str) -> dict:
    data = {}

    patterns = {
        "nombre": r'"goods_name"\s*:\s*"([^"]+)"',
        "serial": r'"goods_sn"\s*:\s*"([^"]+)"',
        "foto_url": r'"goods_img"\s*:\s*"([^"]+)"',
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, html)
        if m:
            data[key] = m.group(1)

    # Precio: probamos varios nombres de clave posibles, del más específico
    # al más genérico, porque los sitios tipo Shein cambian esta estructura
    # seguido. Nos quedamos con el primero que encuentre algo.
    precio_patterns = [
        r'"salePrice"\s*:\s*\{[^}]*?"amountWithSymbol"\s*:\s*"([^"]+)"',
        r'"specialPrice"\s*:\s*\{[^}]*?"amountWithSymbol"\s*:\s*"([^"]+)"',
        r'"discountPrice"\s*:\s*\{[^}]*?"amountWithSymbol"\s*:\s*"([^"]+)"',
        r'"retailPrice"\s*:\s*\{[^}]*?"amountWithSymbol"\s*:\s*"([^"]+)"',
        r'"(?:price|unitPrice|displayPrice|suggestedSalePrice)"\s*:\s*\{[^}]*?"amountWithSymbol"\s*:\s*"([^"]+)"',
        # último recurso: cualquier "amountWithSymbol" que aparezca en la página
        r'"amountWithSymbol"\s*:\s*"([^"]+)"',
        # variante sin "WithSymbol", combinada con el código de moneda aparte
        r'"salePrice"\s*:\s*\{[^}]*?"amount"\s*:\s*"?([\d.]+)"?',
    ]
    for pattern in precio_patterns:
        m = re.search(pattern, html)
        if m:
            data["precio"] = m.group(1)
            break

    tallas = sorted(set(re.findall(r'"attr_value_name"\s*:\s*"([^"]+)"', html)))
    # Filtra ruidos comunes que no son tallas (colores, etc. se cuelan a veces)
    if tallas:
        data["tallas"] = ", ".join(tallas[:15])

    if data.get("foto_url", "").startswith("//"):
        data["foto_url"] = "https:" + data["foto_url"]

    return data


def _fetch_product_region(url: str, region: dict = None, timeout: int = 15) -> ProductResult:
    """Hace UN intento de extracción, opcionalmente simulando una región
    (headers/cookies/proxy de ese país)."""
    result = ProductResult(link=url)
    result._region_code = region["code"] if region else "default"

    cookies = dict(region["cookies"]) if region else {}
    proxies = None
    if region:
        proxy_url = os.environ.get(region["proxy_env"])
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}

    try:
        resp = requests.get(
            url,
            headers=_headers(region),
            cookies=cookies,
            proxies=proxies,
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        result.estado = "error"
        etiqueta = f"[{region['code']}] " if region else ""
        result.detalle_error = f"{etiqueta}No se pudo descargar la página: {exc}"
        return result

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    merged = {}
    merged.update(_from_meta_tags(soup))
    merged.update(_from_json_ld(soup))          # JSON-LD tiene prioridad
    embedded = _from_embedded_json(html)
    for k, v in embedded.items():
        merged.setdefault(k, v)

    if not merged.get("nombre") and soup.title:
        merged["nombre"] = soup.title.get_text(strip=True)

    if not merged.get("serial"):
        merged["serial"] = _goods_id_from_url(url)

    result.nombre = merged.get("nombre", "")
    result.precio = merged.get("precio", "")
    result.tallas = merged.get("tallas", "")
    result.serial = merged.get("serial", "")
    result.foto_url = merged.get("foto_url", "")

    if not any([result.nombre, result.precio, result.foto_url]):
        result.estado = "sin_datos"
        result.detalle_error = (
            f"[{region['code'] if region else 'default'}] La página respondió pero no se "
            "encontraron datos reconocibles (posible bloqueo anti-bot o cambio de estructura)."
        )

    return result


def fetch_product(url: str, timeout: int = 15) -> ProductResult:
    """Extrae los datos de un producto probando distintas 'regiones'
    (EE.UU. primero, luego Venezuela) y combinando los campos que cada
    intento logre traer, para minimizar campos vacíos en el resultado final.
    Se detiene apenas un intento trae todos los campos esenciales."""
    intentos = []
    for region in REGIONS:
        r = _fetch_product_region(url, region=region, timeout=timeout)
        intentos.append(r)
        if _is_complete(r):
            break
    return _merge_results(intentos)


def batch_scrape(urls: list[str], max_workers: int = 6) -> list["ProductResult"]:
    """Procesa varios links EN PARALELO (más rápido que uno por uno).
    max_workers controla cuántas peticiones simultáneas se hacen; subirlo
    acelera el proceso pero aumenta el riesgo de que el sitio bloquee por
    demasiadas peticiones seguidas."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    clean_urls = [u.strip() for u in urls if u.strip()]
    results: list = [None] * len(clean_urls)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(fetch_product, url): i for i, url in enumerate(clean_urls)
        }
        for future in as_completed(future_to_index):
            i = future_to_index[future]
            try:
                results[i] = future.result()
            except Exception as exc:
                results[i] = ProductResult(
                    link=clean_urls[i], estado="error", detalle_error=str(exc)
                )

    return results


def is_shein_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return "shein" in host


def build_url_from_serial(serial: str, domain: str = "us.shein.com") -> str:
    """Arma un link directo de producto a partir de solo el serial/ID
    numérico (el número que aparece después de '-p-' en cualquier link de
    Shein). El texto del nombre en la URL es decorativo: Shein reconoce el
    producto solo con el número y redirige a la página completa igual.

    Nota: esto trae el producto con su talla/color POR DEFECTO (la primera
    opción), no necesariamente la variante exacta que alguien tenía elegida
    al compartir el link original."""
    serial = serial.strip()
    return f"https://{domain}/producto-p-{serial}.html"


def is_probably_serial(text: str) -> bool:
    """True si el texto parece ser solo un ID numérico de producto (serial),
    en vez de un link completo."""
    text = text.strip()
    return text.isdigit() and 5 <= len(text) <= 12
