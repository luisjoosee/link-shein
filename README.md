# Extractor de productos Shein

Herramienta con interfaz web (hecha con [Streamlit](https://streamlit.io)) que recibe
uno o varios links de producto de Shein (hasta 20 a la vez) y extrae:

- Nombre
- Precio
- Tallas disponibles
- Serial / SKU
- Foto

Permite ver los resultados en pantalla y descargarlos en CSV, Excel, o las fotos en un ZIP.

---

## 1. Uso en tu PC (recomendado, más confiable)

Correr el scraper desde tu propia conexión suele funcionar mejor que desde un servidor
gratuito, porque los servicios gratuitos comparten IPs que a veces están bloqueadas por
sitios como Shein.

### Requisitos
- Tener Python 3.9 o superior instalado ([python.org](https://www.python.org/downloads/)).

### Pasos
1. Descarga esta carpeta (`shein_scraper`) a tu computadora.
2. Abre una terminal (CMD, PowerShell o Terminal) dentro de esa carpeta.
3. Instala las dependencias:
   ```bash
   pip install -r requirements.txt
   ```
4. Ejecuta la app:
   ```bash
   streamlit run app.py
   ```
5. Se abrirá automáticamente en tu navegador (normalmente en `http://localhost:8501`).
6. Pega tus links (uno por línea) y presiona **Extraer datos**.

---

## 2. Subirlo gratis a internet (Streamlit Community Cloud)

Así puedes usarlo desde el celular o compartirlo, sin instalar nada en tu PC.

1. Crea una cuenta gratuita en [GitHub](https://github.com) si no tienes.
2. Crea un repositorio nuevo (puede ser privado) y sube estos 3 archivos:
   - `app.py`
   - `scraper.py`
   - `requirements.txt`
3. Ve a [share.streamlit.io](https://share.streamlit.io) e inicia sesión con tu cuenta de GitHub.
4. Click en **"New app"**, selecciona tu repositorio, la rama y el archivo `app.py`.
5. Click en **Deploy**. En un par de minutos tendrás una URL pública tipo
   `https://tu-app.streamlit.app` que puedes abrir desde cualquier dispositivo.

> Nota: en este modo, las peticiones salen desde la IP del servidor de Streamlit Cloud,
> no desde la tuya. Si Shein bloquea esa IP, el scraper puede fallar más seguido que
> corriéndolo localmente. En ese caso, usa la opción 1.

---

## 3. Modo navegador automatizado (para links de "compartir", onelink o carritos)

Los links normales de producto (`...-p-12345678.html`) suelen funcionar con el modo
rápido (peticiones directas). Pero hay otro tipo de links que **no funcionan** con ese
modo porque usan JavaScript para redirigir o cargar los datos después:

- Links de "Compartir producto" desde la app.
- Links `onelink.shein.com/...` (enlaces inteligentes / deep links).
- Links de carrito compartido.

Para estos casos, activa la casilla **"🐢 Usar navegador automatizado"** en la app.
Este modo abre un navegador Chromium real (invisible) que ejecuta el JavaScript de la
página, sigue las redirecciones, y lee los datos ya cargados. Es más lento (varios
segundos por link) pero mucho más confiable con este tipo de enlaces.

### Instalación (una sola vez, en tu PC)
```bash
pip install -r requirements.txt
playwright install chromium
```
En Linux puede que además necesites las librerías del sistema:
```bash
playwright install --with-deps chromium
```

> ⚠️ **Nota sobre despliegue gratuito:** el modo navegador consume más recursos
> (memoria y CPU) que el modo normal. En Streamlit Community Cloud (plan gratis) puede
> ser lento, tardar en la primera carga (~1-2 min descargando Chromium), o incluso
> fallar si el servidor gratuito se queda sin memoria. Si subiste los archivos
> `packages.txt` y `requirements.txt` actualizados, la app debería poder instalar
> Chromium sola la primera vez que la usas — no necesitas hacer nada manual en la nube.
> Si aun así falla seguido, la alternativa más confiable es correr la app en tu propia
> PC (sección 1).

---

## 4. Si Shein bloquea las peticiones (protección anti-bot)

Algunos sitios como Shein usan Cloudflare u otros sistemas que detectan que la petición
no viene de un navegador real. Si notas que muchos links devuelven "sin datos":

- Prueba primero activando el **modo navegador** (sección 3), que resuelve la mayoría
  de estos casos.
- Espera unos minutos y vuelve a intentar (a veces es un bloqueo temporal por IP).
- Reduce la cantidad de links por tanda (por ejemplo, de 5 en 5 en vez de 20).

---

## 5. Sobre carritos con varios productos

Si el link que pegas corresponde a un **carrito compartido** (varios productos en una
sola página), por ahora el scraper está diseñado para leer **un producto por link**, así
que puede traer datos incompletos o del primer producto que detecte en esa página.
Si me confirmas cómo se ve exactamente ese tipo de link cuando lo compartes (o me pasas
uno de ejemplo), puedo adaptar el código para que reconozca cuando un link es un carrito
y extraiga la lista completa de productos que contiene, no solo uno.

---

## Estructura del proyecto

```
shein_scraper/
├── app.py                # Interfaz web (Streamlit)
├── scraper.py             # Lógica de extracción de datos (modo rápido)
├── playwright_scraper.py  # Lógica de extracción con navegador automatizado
├── requirements.txt       # Dependencias de Python
├── packages.txt            # Dependencias de sistema (para que Chromium corra en Streamlit Cloud)
└── README.md
```

## Limitaciones a tener en cuenta

- Este scraper depende de la estructura actual de las páginas de Shein. Si Shein cambia
  su sitio, algunas extracciones pueden dejar de funcionar y habría que ajustar los
  patrones en `scraper.py`.
- Es para uso personal (armar tu lista de compra, comparar precios, etc.). Revisa los
  términos de uso del sitio antes de hacer scraping a gran escala o con fines comerciales.
- Las tallas se extraen de forma heurística (buscando atributos de variante en el HTML);
  en algunos productos puede traer valores extra que no sean tallas (por ejemplo, si el
  producto tiene otras variantes como color).
