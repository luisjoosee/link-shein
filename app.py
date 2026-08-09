import io
import zipfile

import pandas as pd
import requests
import streamlit as st

from scraper import batch_scrape, is_shein_url

st.set_page_config(page_title="Extractor de productos Shein", page_icon="🛍️", layout="wide")

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
        results = []

        # Procesamos uno por uno para poder actualizar la barra de progreso
        from scraper import fetch_product
        import time, random

        for i, url in enumerate(urls):
            progress.progress((i) / len(urls), text=f"Procesando link {i + 1} de {len(urls)}...")
            results.append(fetch_product(url))
            if i < len(urls) - 1:
                time.sleep(random.uniform(1.0, 2.0))

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
                "Serial/SKU": r.serial,
                "Foto (URL)": r.foto_url,
                "Link": r.link,
                "Estado": r.estado,
            }
            for r in results
        ]
    )

    ok_count = sum(1 for r in results if r.estado == "ok")
    st.success(f"{ok_count} de {len(results)} productos procesados correctamente.")

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
                st.write(f"📏 Tallas: {r.tallas or '—'}")
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
