"""
Descarga el inventario desde la API de Mercado Libre (solo lectura)
y actualiza api/data.json con los stocks actuales.
"""
import os
import json
import time
import base64
import requests
from pathlib import Path
from datetime import datetime

BASE_DIR  = Path(__file__).parent.parent
DATA_PATH = BASE_DIR / "api" / "data.json"

CLIENT_ID     = os.environ["ML_CLIENT_ID"]
CLIENT_SECRET = os.environ["ML_CLIENT_SECRET"]
USER_ID       = os.environ["ML_USER_ID"]
REFRESH_TOKEN = os.environ["ML_REFRESH_TOKEN"]
GITHUB_PAT    = os.environ.get("GITHUB_PAT", "")
GITHUB_REPO   = "jmvaldes321/justpets-logistics"


# ── Token ──────────────────────────────────────────────────────────────────

def refresh_access_token() -> dict:
    """Obtiene un nuevo access token usando el refresh token."""
    resp = requests.post("https://api.mercadolibre.com/oauth/token", data={
        "grant_type":    "refresh_token",
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def update_github_secret(secret_name: str, secret_value: str):
    """Actualiza un secret de GitHub Actions via API (para rotar tokens)."""
    if not GITHUB_PAT:
        return
    try:
        from nacl import encoding, public as nacl_public
        headers = {
            "Authorization": f"Bearer {GITHUB_PAT}",
            "Accept": "application/vnd.github+json",
        }
        key_resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/public-key",
            headers=headers, timeout=10
        )
        key_data = key_resp.json()
        pk = nacl_public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
        encrypted = base64.b64encode(nacl_public.SealedBox(pk).encrypt(secret_value.encode())).decode()
        requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/{secret_name}",
            headers=headers,
            json={"encrypted_value": encrypted, "key_id": key_data["key_id"]},
            timeout=10
        )
        print(f"✓ GitHub secret {secret_name} actualizado")
    except Exception as e:
        print(f"  Aviso: no se pudo actualizar secret {secret_name}: {e}")


# ── ML API ─────────────────────────────────────────────────────────────────

def get_all_item_ids(access_token: str) -> list[str]:
    """Obtiene todos los IDs de publicaciones activas del vendedor."""
    headers = {"Authorization": f"Bearer {access_token}"}
    all_ids = []
    limit = 100
    offset = 0

    while True:
        r = requests.get(
            f"https://api.mercadolibre.com/users/{USER_ID}/items/search",
            params={"status": "active", "limit": limit, "offset": offset},
            headers=headers, timeout=15
        )
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])
        all_ids.extend(results)

        total = data.get("paging", {}).get("total", 0)
        offset += limit
        print(f"  Items cargados: {len(all_ids)} / {total}")
        if offset >= total or not results:
            break
        time.sleep(0.2)  # respetar rate limit

    return all_ids


def get_items_detail(item_ids: list[str], access_token: str) -> list[dict]:
    """Obtiene detalles (título, SKU, stock) en batches de 20."""
    headers = {"Authorization": f"Bearer {access_token}"}
    items = []
    attrs = "id,title,available_quantity,seller_sku,category_id,price,condition"

    for i in range(0, len(item_ids), 20):
        batch = item_ids[i:i+20]
        r = requests.get(
            "https://api.mercadolibre.com/items",
            params={"ids": ",".join(batch), "attributes": attrs},
            headers=headers, timeout=15
        )
        r.raise_for_status()
        for entry in r.json():
            if entry.get("code") == 200:
                items.append(entry["body"])
        time.sleep(0.1)

    return items


# ── data.json ──────────────────────────────────────────────────────────────

def update_data_json(ml_items: list[dict]):
    """
    Actualiza api/data.json con el stock de ML.
    Solo actualiza inventario y m3_totales de productos existentes.
    La planilla maestra es la fuente de verdad — nunca se agregan nuevos desde ML.
    """
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        products = json.load(f)

    # Índices para búsqueda rápida
    by_sku   = {p["sku"]: p for p in products}
    by_ml_id = {p.get("ml_item_id"): p for p in products if p.get("ml_item_id")}

    updated     = 0
    not_matched = 0

    for item in ml_items:
        ml_id = item.get("id", "")
        sku   = str(item.get("seller_sku") or "").strip() or ml_id
        qty   = int(item.get("available_quantity") or 0)

        product = by_sku.get(sku) or by_ml_id.get(ml_id)

        if product:
            old_qty = product["inventario"]
            product["inventario"] = qty
            product["m3_totales"] = round(product.get("m3_producto", 0) * qty, 6)
            product["ml_item_id"] = ml_id
            if old_qty != qty:
                updated += 1
        else:
            not_matched += 1

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False)

    total_m3  = sum(p["m3_totales"] for p in products)
    con_stock = sum(1 for p in products if p["inventario"] > 0)
    print(f"✓ data.json: {updated} actualizados  |  {not_matched} de ML sin match en planilla")
    print(f"✓ Total productos: {len(products)}  |  Con stock: {con_stock}")
    print(f"✓ M³ total: {total_m3:.4f}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*50}")
    print(f"Sync ML inventario — {ts}")
    print(f"{'='*50}\n")

    print("→ Renovando access token...")
    token_data    = refresh_access_token()
    access_token  = token_data["access_token"]
    refresh_token = token_data.get("refresh_token", REFRESH_TOKEN)
    print(f"✓ Token válido ({token_data.get('expires_in', 0)//3600}h)")

    # Rotar tokens en GitHub secrets si cambiaron
    update_github_secret("ML_ACCESS_TOKEN",  access_token)
    update_github_secret("ML_REFRESH_TOKEN", refresh_token)

    print("\n→ Obteniendo publicaciones activas de ML...")
    item_ids = get_all_item_ids(access_token)
    print(f"✓ Total publicaciones: {len(item_ids)}")

    print("\n→ Descargando detalles de productos...")
    items = get_items_detail(item_ids, access_token)
    print(f"✓ Detalles obtenidos: {len(items)}")

    print("\n→ Actualizando data.json...")
    update_data_json(items)

    print("\n✓ Sync completo")


if __name__ == "__main__":
    main()
