from flask import Flask, jsonify, request, redirect
from flask_cors import CORS
import json
import os
import time
import requests as http_requests

GITHUB_REPO  = "jmvaldes321/justpets-logistics"
GITHUB_WORKFLOW = "sync_inventory.yml"

app = Flask(__name__)
CORS(app)

DATA_PATH = os.path.join(os.path.dirname(__file__), 'data.json')

def load_data():
    with open(DATA_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)

@app.route('/api/summary')
def summary():
    products = load_data()
    total_m3 = sum(p['m3_totales'] for p in products)
    total_inventario = sum(p['inventario'] for p in products)
    total_productos = len(products)

    # M3 por categoria
    por_categoria = {}
    for p in products:
        cat = p['categoria']
        if cat not in por_categoria:
            por_categoria[cat] = {'m3': 0, 'inventario': 0, 'productos': 0}
        por_categoria[cat]['m3'] += p['m3_totales']
        por_categoria[cat]['inventario'] += p['inventario']
        por_categoria[cat]['productos'] += 1

    con_stock = sum(1 for p in products if p['inventario'] > 0)

    categorias_lista = sorted(
        [{'categoria': k, **v} for k, v in por_categoria.items()],
        key=lambda x: x['m3'],
        reverse=True
    )

    return jsonify({
        'total_m3': round(total_m3, 4),
        'total_inventario': total_inventario,
        'total_productos': total_productos,
        'con_stock': con_stock,
        'por_categoria': categorias_lista
    })

@app.route('/api/categories')
def categories():
    products = load_data()
    cats = sorted(set(p['categoria'] for p in products))
    return jsonify(cats)

@app.route('/api/products')
def products_endpoint():
    products = load_data()

    # Filtros
    categoria = request.args.get('categoria', '').strip()
    search = request.args.get('search', '').strip().lower()
    sort_by = request.args.get('sort', 'm3_totales')
    order = request.args.get('order', 'desc')
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 50))

    if categoria:
        products = [p for p in products if p['categoria'] == categoria]

    if search:
        products = [p for p in products if search in p['nombre'].lower() or search in p['sku']]

    # Ordenar
    valid_sorts = ['m3_totales', 'm3_producto', 'inventario', 'peso_kg', 'nombre']
    if sort_by not in valid_sorts:
        sort_by = 'm3_totales'

    products = sorted(products, key=lambda x: x[sort_by] if isinstance(x[sort_by], (int, float)) else str(x[sort_by]), reverse=(order == 'desc'))

    total = len(products)
    start = (page - 1) * limit
    end = start + limit

    return jsonify({
        'total': total,
        'page': page,
        'limit': limit,
        'pages': (total + limit - 1) // limit,
        'data': products[start:end]
    })

@app.route('/api/products/<path:sku>', methods=['PUT'])
def update_product(sku):
    body = request.json or {}
    products = load_data()

    idx = next((i for i, p in enumerate(products) if p['sku'] == sku), None)
    if idx is None:
        return jsonify({'error': 'Producto no encontrado'}), 404

    p = products[idx]

    if 'nombre' in body:
        p['nombre'] = str(body['nombre']).strip()
    if 'sku' in body:
        p['sku'] = str(body['sku']).strip()
    if 'peso_kg' in body:
        p['peso_kg'] = float(body['peso_kg'])
    if 'm3_x_kg' in body:
        p['m3_x_kg'] = float(body['m3_x_kg'])
    if 'm3_producto' in body:
        p['m3_producto'] = float(body['m3_producto'])

    p['m3_totales'] = round(p['m3_producto'] * p['inventario'], 6)

    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(products, f, ensure_ascii=False)

    return jsonify(p)


STEP_LABELS = {
    "Set up job":                       None,
    "Checkout repo":                    None,
    "Setup Python":                     None,
    "Post Checkout repo":               None,
    "Post Setup Python":                None,
    "Complete job":                     None,
    "Install dependencias":             "Instalando dependencias",
    "Ejecutar sync de inventario":      "Leyendo inventario de Mercado Libre",
    "Commit y push data.json actualizado": "Publicando cambios",
}

def _gh_headers():
    token = os.environ.get("GITHUB_PAT", "")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

@app.route('/api/sync/trigger', methods=['POST'])
def sync_trigger():
    try:
        if not os.environ.get("GITHUB_PAT"):
            return jsonify({'error': 'GITHUB_PAT no configurado'}), 500

        resp = http_requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW}/dispatches",
            headers=_gh_headers(),
            json={"ref": "main"},
            timeout=15
        )
        if resp.status_code != 204:
            return jsonify({'error': f'GitHub API: {resp.status_code} {resp.text}'}), 502

        time.sleep(4)
        runs_resp = http_requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?event=workflow_dispatch&per_page=1",
            headers=_gh_headers(), timeout=10
        )
        runs = runs_resp.json().get('workflow_runs', [])
        run_id = runs[0]['id'] if runs else None
        return jsonify({'run_id': run_id, 'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sync/status')
def sync_status():
    run_id = request.args.get('run_id')
    if not run_id:
        return jsonify({'error': 'run_id requerido'}), 400

    run_resp = http_requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{run_id}",
        headers=_gh_headers(), timeout=10
    )
    if run_resp.status_code != 200:
        return jsonify({'error': 'Run no encontrado'}), 404
    run = run_resp.json()

    jobs_resp = http_requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{run_id}/jobs",
        headers=_gh_headers(), timeout=10
    )
    jobs_data = jobs_resp.json().get('jobs', [])

    steps = []
    if jobs_data:
        for step in jobs_data[0].get('steps', []):
            label = STEP_LABELS.get(step['name'], step['name'])
            if label is None:
                continue
            steps.append({
                'name': label,
                'status': step['status'],        # queued | in_progress | completed
                'conclusion': step.get('conclusion'),  # success | failure | None
            })

    return jsonify({
        'run_id': run['id'],
        'status': run['status'],
        'conclusion': run.get('conclusion'),
        'created_at': run.get('created_at'),
        'steps': steps,
    })


ML_CLIENT_ID     = os.environ.get("ML_CLIENT_ID", "")
ML_CLIENT_SECRET = os.environ.get("ML_CLIENT_SECRET", "")
ML_REDIRECT_URI  = "https://justpets-logistics.vercel.app/api/auth/callback"
ML_AUTH_URL      = "https://auth.mercadolibre.cl/authorization"
ML_TOKEN_URL     = "https://api.mercadolibre.com/oauth/token"


@app.route('/api/auth/ml')
def ml_auth():
    url = (
        f"{ML_AUTH_URL}?response_type=code"
        f"&client_id={ML_CLIENT_ID}"
        f"&redirect_uri={ML_REDIRECT_URI}"
    )
    return redirect(url)


@app.route('/api/auth/callback')
def ml_callback():
    code = request.args.get('code')
    error = request.args.get('error')

    if error:
        return f"<h2>Error de autorización: {error}</h2>", 400
    if not code:
        return "<h2>No se recibió código de autorización.</h2>", 400

    resp = http_requests.post(ML_TOKEN_URL, data={
        'grant_type':    'authorization_code',
        'client_id':     ML_CLIENT_ID,
        'client_secret': ML_CLIENT_SECRET,
        'code':          code,
        'redirect_uri':  ML_REDIRECT_URI,
    }, timeout=15)

    if resp.status_code != 200:
        return f"<h2>Error al obtener token: {resp.status_code}</h2><pre>{resp.text}</pre>", 400

    tokens = resp.json()
    access_token  = tokens.get('access_token', '')
    refresh_token = tokens.get('refresh_token', '')
    user_id       = tokens.get('user_id', '')
    expires_in    = tokens.get('expires_in', 21600)

    return f"""
    <html><body style="font-family:monospace;padding:2rem;background:#f0fdf4">
    <h2 style="color:#16a34a">✓ Autorización exitosa</h2>
    <p>Usuario ML: <b>{user_id}</b></p>
    <p>Ahora ejecuta estos comandos en tu terminal para guardar los tokens:</p>
    <pre style="background:#1e293b;color:#f8fafc;padding:1rem;border-radius:8px">
printf '{access_token}' | vercel env add ML_ACCESS_TOKEN production --yes
printf '{refresh_token}' | vercel env add ML_REFRESH_TOKEN production --yes
printf '{user_id}' | vercel env add ML_USER_ID production --yes
    </pre>
    <p style="color:#64748b">El access token expira en {expires_in // 3600}h. El refresh token se renueva automáticamente.</p>
    </body></html>
    """


@app.route('/api/ml/notifications', methods=['POST'])
def ml_notifications():
    """Webhook de Mercado Libre para notificaciones en tiempo real."""
    return jsonify({'status': 'ok'}), 200


if __name__ == '__main__':
    app.run(debug=True, port=5001)
