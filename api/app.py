from flask import Flask, jsonify, request
from flask_cors import CORS
import json
import os

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

    categorias_lista = sorted(
        [{'categoria': k, **v} for k, v in por_categoria.items()],
        key=lambda x: x['m3'],
        reverse=True
    )

    return jsonify({
        'total_m3': round(total_m3, 4),
        'total_inventario': total_inventario,
        'total_productos': total_productos,
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

if __name__ == '__main__':
    app.run(debug=True, port=5001)
