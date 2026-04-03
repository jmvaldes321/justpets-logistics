"""
Descarga el reporte consolidado de inventario desde logystix.co,
procesa el Excel y actualiza api/data.json con los nuevos stocks.
"""
import asyncio
import os
import json
import glob
import sys
import tempfile
from pathlib import Path
from datetime import datetime

import openpyxl
import pandas as pd
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout


BASE_DIR = Path(__file__).parent.parent
DATA_PATH = BASE_DIR / "api" / "data.json"

USERNAME = os.environ["LOGYSTIX_USER"]
PASSWORD = os.environ["LOGYSTIX_PASSWORD"]


async def download_report(download_dir: str) -> str:
    """Navega logystix.co, genera el reporte y descarga el Excel. Retorna la ruta del archivo."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = await browser.new_context(
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()

        print("→ Abriendo login...")
        await page.goto("https://ok.logystix.co/site/login", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await page.screenshot(path="step1_login_page.png")
        print(f"  URL actual: {page.url}")

        # Login
        await page.fill("#username-input", USERNAME)
        await page.fill("#password-input", PASSWORD)
        await page.screenshot(path="step2_filled.png")

        await page.locator("button[type='submit'], .btn-login, input[type='submit'], button:has-text('Entrar')").first.click()
        # Esperar a que el SPA cargue el dashboard completamente
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.wait_for_timeout(3000)
        await page.screenshot(path="step3_after_login.png")
        print(f"  URL post-login: {page.url}")

        # Detectar login exitoso: no hay formulario de login visible
        login_form_visible = await page.locator("#username-input").is_visible()
        if login_form_visible:
            raise RuntimeError("Login fallido — el formulario sigue visible. Ver step3_after_login.png")
        print("✓ Login exitoso")

        # Esperar a que el sidebar cargue completamente
        await page.wait_for_timeout(4000)
        await page.screenshot(path="step3b_dashboard_loaded.png")

        # Dump de todos los textos visibles del sidebar para debugging
        all_links = await page.locator("a, [role='menuitem'], nav span, aside span, .menu-item, li a").all_text_contents()
        menu_texts = [t.strip() for t in all_links if t.strip()]
        print(f"  Textos del menú: {menu_texts[:40]}")

        # Navegar a Reporte de Inventarios → Reporte Consolidado
        print("→ Navegando a Reporte consolidado de inventarios...")
        # Probar múltiples variantes del texto del menú
        nav1_options = ["Reportes de Inventario", "Reporte de Inventario", "Inventario"]
        nav1_clicked = False
        for text in nav1_options:
            try:
                locator = page.get_by_text(text, exact=False).first
                if await locator.is_visible():
                    await locator.click(timeout=5000)
                    await page.wait_for_load_state("networkidle")
                    await page.wait_for_timeout(1500)
                    nav1_clicked = True
                    print(f"✓ Clic en '{text}'")
                    break
            except Exception:
                continue

        if not nav1_clicked:
            await page.screenshot(path="nav_error.png")
            raise RuntimeError(f"No se encontró ninguna opción de menú de inventario. Textos disponibles: {menu_texts[:30]}")

        await page.screenshot(path="step4_menu_inventario.png")

        try:
            await page.get_by_text("Reporte consolidado", exact=False).first.click(timeout=10000)
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(1500)
        except PlaywrightTimeout:
            # Tomar screenshot y mostrar textos disponibles
            all_texts = await page.locator("a, button, li").all_text_contents()
            available = [t.strip() for t in all_texts if t.strip()]
            await page.screenshot(path="nav2_error.png")
            raise RuntimeError(f"No se encontró 'Reporte consolidado'. Textos disponibles: {available[:30]}")

        print("✓ En página de reporte consolidado")
        await page.screenshot(path="debug_reporte.png")

        # Seleccionar bodega "ebox layout"
        print("→ Seleccionando bodega 'ebox layout'...")
        bodega_seleccionada = False

        # 1. Forzar selección vía JS en el native <select> + trigger Select2
        try:
            selected = await page.evaluate("""() => {
                const selects = document.querySelectorAll('select');
                for (const sel of selects) {
                    for (const opt of sel.options) {
                        if (opt.text.toLowerCase().includes('ebox')) {
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', { bubbles: true }));
                            if (window.jQuery && window.jQuery(sel).data('select2')) {
                                window.jQuery(sel).trigger('change');
                            }
                            return opt.text;
                        }
                    }
                }
                return null;
            }""")
            if selected:
                bodega_seleccionada = True
                print(f"✓ Bodega (JS + Select2): {selected.strip()}")
                await page.wait_for_timeout(500)
        except Exception as e:
            print(f"  JS select fallido: {e}")

        # 2. Fallback: abrir Select2 y buscar "ebox"
        if not bodega_seleccionada:
            try:
                await page.locator(".select2-selection, .select2-container").first.click()
                await page.wait_for_timeout(600)
                search_input = page.locator(".select2-search__field")
                if await search_input.is_visible():
                    await search_input.fill("ebox")
                    await page.wait_for_timeout(400)
                await page.locator(".select2-results__option:has-text('ebox')").first.click(timeout=5000)
                bodega_seleccionada = True
                print("✓ Bodega (Select2 UI): ebox layout")
            except Exception as e:
                print(f"  Select2 UI fallido: {e}")

        if not bodega_seleccionada:
            await page.screenshot(path="bodega_error.png")
            raise RuntimeError("No se pudo seleccionar bodega. Ver bodega_error.png")

        await page.screenshot(path="step5_bodega_selected.png")

        # Generar reporte
        print("→ Generando reporte...")
        try:
            await page.get_by_text("Generar Reporte", exact=False).first.click()
            await page.wait_for_load_state("networkidle", timeout=60000)
            await page.wait_for_timeout(3000)
        except PlaywrightTimeout:
            await page.screenshot(path="generar_error.png")
            raise RuntimeError("Timeout al generar reporte. Ver generar_error.png")
        await page.screenshot(path="step6_reporte_generado.png")
        print("✓ Reporte generado")

        # Descargar Excel
        # La tabla "Descarga de Reportes" muestra los reportes generados.
        # El más reciente (primer fila) tiene el botón de descarga en la columna ACCIONES.
        print("→ Iniciando descarga del Excel...")
        await page.screenshot(path="step7_before_download.png")

        async with page.expect_download(timeout=90000) as dl_info:
            download_triggered = False
            # 1. Botón de la primera fila en la tabla de reportes (más reciente)
            try:
                first_row_btn = page.locator("table tbody tr:first-child td:last-child a, table tbody tr:first-child td:last-child button").first
                if await first_row_btn.is_visible(timeout=3000):
                    await first_row_btn.click()
                    download_triggered = True
                    print("✓ Click en botón de la primera fila de la tabla")
            except Exception as e:
                print(f"  Primer fila fallida: {e}")

            # 2. Buscar cualquier link/botón de descarga por atributo href o texto
            if not download_triggered:
                try:
                    await page.locator("a[href*='download'], a[href*='excel'], a[download]").first.click(timeout=5000)
                    download_triggered = True
                    print("✓ Click en link de descarga")
                except Exception:
                    pass

            # 3. Buscar botón naranja/acción en la tabla
            if not download_triggered:
                try:
                    await page.locator("table .btn, table button, table a").first.click(timeout=5000)
                    download_triggered = True
                    print("✓ Click en botón de tabla")
                except Exception:
                    pass

            if not download_triggered:
                await page.screenshot(path="download_error.png")
                all_texts = await page.locator("table a, table button").all_text_contents()
                raise RuntimeError(f"No se encontró botón de descarga. Botones en tabla: {all_texts[:10]}")

        download = await dl_info.value
        dest = os.path.join(download_dir, download.suggested_filename or "inventario.xlsx")
        await download.save_as(dest)
        print(f"✓ Descargado: {dest}")

        await browser.close()
        return dest


def parse_inventory_excel(path: str) -> dict:
    """
    Equivalente a una tabla dinámica clásica sin subtotales:
    agrupa por SKU y suma las cantidades, ignorando filas de subtotal/total.
    """
    # Leer el Excel completo con pandas
    df = pd.read_excel(path, header=None, dtype=str)

    # Encontrar la fila de encabezados (primera fila con datos)
    header_row = None
    for i, row in df.iterrows():
        vals = row.dropna().tolist()
        if len(vals) >= 2:
            header_row = i
            break

    if header_row is None:
        raise RuntimeError("No se encontró fila de encabezados en el Excel")

    df.columns = [str(c).strip().lower() if pd.notna(c) else f"col_{i}" for i, c in enumerate(df.iloc[header_row])]
    df = df.iloc[header_row + 1:].reset_index(drop=True)
    print(f"  Headers del Excel: {list(df.columns)}")

    def find_col(keywords):
        for col in df.columns:
            if any(k in col for k in keywords):
                return col
        return None

    sku_col  = find_col(["ean", "sku", "codigo", "código", "barcode", "cod"])
    qty_col  = find_col(["cantidad", "stock", "inventario", "qty", "unidades", "disponible", "saldo"])
    name_col = find_col(["nombre", "producto", "descripcion", "descripción", "name"])
    peso_col = find_col(["peso", "weight", "kg"])

    if sku_col is None or qty_col is None:
        raise RuntimeError(f"No se encontraron columnas SKU/cantidad. Columnas: {list(df.columns)}")

    print(f"→ SKU: '{sku_col}'  |  Cantidad: '{qty_col}'")

    # Limpiar SKU: quitar filas vacías, subtotales y totales generales
    df[sku_col] = df[sku_col].astype(str).str.strip()
    df[qty_col] = pd.to_numeric(df[qty_col], errors='coerce').fillna(0)

    # Filtrar filas de subtotal/total: SKU vacío, 'nan', 'total', 'subtotal', etc.
    mask_valido = (
        df[sku_col].notna() &
        ~df[sku_col].isin(['', 'nan', 'none', 'NaN']) &
        ~df[sku_col].str.lower().str.contains('total|subtotal|suma|grand', na=False)
    )
    df = df[mask_valido].copy()

    # Normalizar SKU: quitar decimales (ej. "8436538947630.0" → "8436538947630")
    df[sku_col] = df[sku_col].str.split('.').str[0]

    # --- TABLA DINÁMICA: agrupar por SKU, sumar cantidad ---
    pivot = df.groupby(sku_col, as_index=False).agg(
        cantidad=(qty_col, 'sum'),
        **({'nombre': (name_col, 'first')} if name_col else {}),
        **({'peso_kg': (peso_col, 'first')} if peso_col else {}),
    )
    pivot['cantidad'] = pivot['cantidad'].astype(int)

    inventory = {}
    for _, row in pivot.iterrows():
        sku = str(row[sku_col]).strip()
        entry = {"cantidad": int(row['cantidad'])}
        if 'nombre' in pivot.columns and pd.notna(row.get('nombre')):
            entry['nombre'] = str(row['nombre']).strip()
        if 'peso_kg' in pivot.columns and pd.notna(row.get('peso_kg')):
            try:
                entry['peso_kg'] = float(row['peso_kg'])
            except Exception:
                pass
        inventory[sku] = entry

    skus_con_stock = sum(1 for e in inventory.values() if e['cantidad'] > 0)
    print(f"✓ Tabla dinámica: {len(inventory)} SKUs únicos  |  {skus_con_stock} con stock > 0")
    return inventory


def update_data_json(inventory: dict):
    """
    Actualiza api/data.json:
    - Actualiza inventario y M3 de productos existentes.
    - Agrega SKUs nuevos que estén en el reporte pero no en data.json.
    """
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        products = json.load(f)

    existing_skus = {p["sku"] for p in products}
    updated = 0
    added = 0
    not_found = 0

    # Actualizar productos existentes
    for p in products:
        sku = p["sku"]
        if sku in inventory:
            old_inv = p["inventario"]
            new_inv = inventory[sku]["cantidad"]
            p["inventario"] = new_inv
            p["m3_totales"] = round(p["m3_producto"] * new_inv, 6)
            if old_inv != new_inv:
                updated += 1
        else:
            not_found += 1

    # Agregar SKUs nuevos del reporte que no están en data.json
    for sku, data in inventory.items():
        if sku not in existing_skus:
            qty = data["cantidad"]
            nombre = data.get("nombre", sku)
            peso = data.get("peso_kg", 0)
            products.append({
                "sku": sku,
                "nombre": nombre,
                "inventario": qty,
                "peso_kg": peso,
                "m3_x_kg": 0,
                "m3_producto": 0,
                "m3_totales": 0,
                "categoria": "SIN CATEGORIA"
            })
            added += 1

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False)

    total_m3 = sum(p["m3_totales"] for p in products)
    print(f"✓ data.json: {updated} actualizados, {added} nuevos agregados, {not_found} no en reporte")
    print(f"✓ Total SKUs en data.json: {len(products)}")
    print(f"✓ M³ total bodega: {total_m3:.4f}")


async def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*50}")
    print(f"Sync inventario — {ts}")
    print(f"{'='*50}\n")

    with tempfile.TemporaryDirectory() as tmp:
        excel_path = await download_report(tmp)
        inventory = parse_inventory_excel(excel_path)
        update_data_json(inventory)

    print("\n✓ Sync completo")


if __name__ == "__main__":
    asyncio.run(main())
