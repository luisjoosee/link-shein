import io
import zipfile
import subprocess
import sys

import pandas as pd
import requests
import streamlit as st

from scraper import batch_scrape, is_shein_url, merge_into

st.set_page_config(page_title="Extractor de productos Shein", page_icon="🛍️", layout="wide")


@st.cache_resource(show_spinner=False)
def ensure_playwright_browser():
    """Descarga el navegador Chromium para Playwright una sola vez.
    Necesario porque en Streamlit Cloud no hay forma de correrlo manualmente
    como en una PC local."""
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return True, ""
    except Exception as exc:
        return False, str(exc)


st.title("🛍️ Extractor de productos Shein")
st.caption(
    "Pega uno o varios links de producto de Shein (uno por línea, hasta 20). "
    "Se extraerá: nombre, precio, tallas, SKU/serial y foto."
)

with st.expander("⚠️ Aviso importante", expanded=False):
    st.write(
        "Shein puede bloquear peticiones automatizadas. Si un link no trae datos, "
        "prueba de nuevo más tarde, revisa que el link sea correcto, o usa el modo "
        "con navegador (ver README) para casos difíciles."
    )

default_placeholder = "https://www.shein.com/...-p-12345678.html\nhttps://www.shein.com/...-p-87654321.html"
links_text = st.text_area("Links de producto", height=180, placeholder=default_placeholder)

usar_navegador = st.radio(
    "Modo de extracción",
    options=["automatico", "rapido", "navegador"],
    format_func=lambda x: {
        "automatico": "⚡ Automático (recomendado): rápido primero, navegador solo si hace falta",
        "rapido": "🚀 Solo modo rápido (no funciona con links de compartir/onelink/carrito)",
        "navegador": "🐢 Forzar navegador para todos (más lento, útil para carritos)",
    }[x],
    index=0,
)

col1, col2 = st.columns([1, 5])
with col1:
    run = st.button("🔍 Extraer datos", type="primary")

if "results" not in st.session_state:
    st.session_state["results"] = []

if run:
    urls = [u.strip() for u in links_text.splitlines() if u.strip()]
    urls = urls[:20]  # límite de seguridad

    if not urls:
        st.warning("Pega al menos un link.")
    else:
        no_shein = [u for u in urls if not is_shein_url(u)]
        if no_shein:
            st.info(
                f"{len(no_shein)} link(s) no parecen ser de shein.com; "
                "se intentarán igual, pero pueden no funcionar."
            )

        progress = st.progress(0, text="Iniciando...")

        def necesita_navegador():
            try:
                from playwright_scraper import batch_scrape_playwright  # noqa: F401
                return True
            except ImportError:
                return False

        if usar_navegador == "rapido":
            progress.progress(0.2, text="Extrayendo (modo rápido, en paralelo)...")
            results = batch_scrape(urls)
            progress.progress(1.0, text="¡Listo!")

        elif usar_navegador == "navegador":
            if not necesita_navegador():
                st.error(
                    "El modo navegador necesita Playwright instalado (revisa requirements.txt)."
                )
                results = []
            else:
                with st.spinner("Preparando navegador automatizado (solo la primera vez, puede tardar 1-2 min)..."):
                    ok, err = ensure_playwright_browser()
                if not ok:
                    st.error(
                        f"No se pudo preparar el navegador automatizado en este servidor: {err}\n\n"
                        "Esto funciona de forma más confiable corriendo la app en tu propia PC."
                    )
                    results = []
                else:
                    from playwright_scraper import batch_scrape_playwright

                    def cb(done, total):
                        progress.progress(done / total, text=f"Navegador: {done}/{total} links...")

                    results = batch_scrape_playwright(urls, progress_callback=cb)
                    progress.progress(1.0, text="¡Listo!")

        else:  # automatico
            progress.progress(0.1, text="Paso 1/2: intentando modo rápido para todos los links...")
            results = batch_scrape(urls)

            fallidos_idx = [i for i, r in enumerate(results) if r.estado != "ok"]

            if fallidos_idx and necesita_navegador():
                with st.spinner(
                    f"{len(fallidos_idx)} link(s) necesitan navegador automatizado, preparando..."
                ):
                    ok, err = ensure_playwright_browser()

                if ok:
                    from playwright_scraper import batch_scrape_playwright

                    urls_fallidos = [results[i].link for i in fallidos_idx]

                    def cb(done, total):
                        progress.progress(
                            0.3 + 0.7 * (done / total),
                            text=f"Paso 2/2: navegador para casos difíciles ({done}/{total})...",
                        )

                    reintentos = batch_scrape_playwright(urls_fallidos, progress_callback=cb)
                    for idx, nuevo in zip(fallidos_idx, reintentos):
                        results[idx] = merge_into(results[idx], nuevo)
                else:
                    st.info(
                        f"No se pudo usar el navegador automatizado para reintentar "
                        f"{len(fallidos_idx)} link(s) difíciles: {err}"
                    )

            progress.progress(1.0, text="¡Listo!")

        st.session_state["results"] = results

results = st.session_state["results"]

if results:
    df = pd.DataFrame(
        [
            {
                "Nombre": r.nombre,
                "Precio": r.precio,
                "Tallas": r.tallas,
                "Color": r.color,
                "Serial/SKU": r.serial,
                "Foto (URL)": r.foto_url,
                "Link": r.link,
                "Estado": r.estado,
            }
            for r in results
        ]
    )

    ok_count = sum(1 for r in results if r.estado == "ok")
    incompletos_count = sum(1 for r in results if r.estado == "incompleto")
    fallidos_count = len(results) - ok_count - incompletos_count

    msg = f"{ok_count} de {len(results)} productos completos."
    if incompletos_count:
        msg += f" {incompletos_count} con algún campo faltante."
    if fallidos_count:
        msg += f" {fallidos_count} sin datos."
    if fallidos_count == 0:
        st.success(msg)
    else:
        st.warning(msg)

    st.subheader("Vista previa")
    for r in results:
        with st.container(border=True):
            c1, c2 = st.columns([1, 4])
            with c1:
                if r.foto_url:
                    try:
                        st.image(r.foto_url, width=140)
                    except Exception:
                        st.write("🖼️ (no se pudo cargar la imagen)")
                else:
                    st.write("🖼️ Sin imagen")
            with c2:
                st.markdown(f"**{r.nombre or '(sin nombre)'}**")
                st.write(f"💲 Precio: {r.precio or '—'}")
                st.write(f"📏 Talla: {r.tallas or '—'}")
                st.write(f"🎨 Color: {r.color or '—'}")
                st.write(f"🔢 Serial/SKU: {r.serial or '—'}")
                st.write(f"🔗 [Ver producto]({r.link})")
                if r.estado != "ok":
                    st.warning(r.detalle_error)

    st.subheader("Descargar resultados")

    d1, d2, d3 = st.columns(3)

    with d1:
        csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "⬇️ Descargar CSV",
            data=csv_bytes,
            file_name="productos_shein.csv",
            mime="text/csv",
        )

    with d2:
        excel_buffer = io.BytesIO()
        with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Productos")
        st.download_button(
            "⬇️ Descargar Excel",
            data=excel_buffer.getvalue(),
            file_name="productos_shein.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with d3:
        if st.button("📦 Preparar ZIP de fotos"):
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w") as zf:
                for i, r in enumerate(results, start=1):
                    if not r.foto_url:
                        continue
                    try:
                        img_resp = requests.get(r.foto_url, timeout=10)
                        img_resp.raise_for_status()
                        ext = r.foto_url.split(".")[-1].split("?")[0][:4] or "jpg"
                        safe_name = (r.serial or f"producto_{i}").replace("/", "_")
                        zf.writestr(f"{safe_name}.{ext}", img_resp.content)
                    except Exception:
                        continue
            st.download_button(
                "⬇️ Descargar ZIP de fotos",
                data=zip_buffer.getvalue(),
                file_name="fotos_shein.zip",
                mime="application/zip",
            )
else:
    st.info("Los resultados aparecerán aquí después de extraer los datos.")
