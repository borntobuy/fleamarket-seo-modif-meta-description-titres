from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
import requests
import hashlib
import base64
import os
import secrets
import json

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

# Stockage temporaire en mémoire (tokens Etsy, PKCE state)
etsy_state_store = {}  # state -> {verifier, ...}
_etsy_tok = _load_token('etsy')
etsy_token_store = {'current': _etsy_tok} if _etsy_tok else {}  # 'current' -> {access_token, refresh_token, expires_at}

# ── Page principale ────────────────────────────────────────────────────────
@app.route('/')
def index():
    return app.send_static_file('index.html')

# ── Proxy eBay XML ─────────────────────────────────────────────────────────
@app.route('/ebay', methods=['POST'])
def ebay_proxy():
    data = request.json
    headers = data.get('headers', {})
    body = data.get('body', '')
    try:
        resp = requests.post(
            'https://api.ebay.com/ws/api.dll',
            headers=headers,
            data=body.encode('utf-8'),
            timeout=30
        )
        return jsonify({'status': resp.status_code, 'body': resp.text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Proxy eBay REST ────────────────────────────────────────────────────────
@app.route('/ebay_rest', methods=['POST'])
def ebay_rest_proxy():
    data = request.json
    path = data.get('path', '')
    method = data.get('method', 'GET')
    token = data.get('token', '')
    body = data.get('body', None)
    try:
        resp = requests.request(
            method,
            f'https://api.ebay.com{path}',
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            },
            json=body,
            timeout=30
        )
        try:
            json_data = resp.json()
        except Exception:
            json_data = {'raw': resp.text[:500]}
        return jsonify({'status': resp.status_code, 'body': resp.text[:2000], 'json': json_data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Proxy Anthropic ────────────────────────────────────────────────────────
@app.route('/anthropic', methods=['POST'])
def anthropic_proxy():
    data = request.json
    api_key = data.get('api_key', '').strip()
    payload = data.get('payload', {})
    if not api_key:
        return jsonify({'error': {'message': 'Clé Anthropic manquante — vérifiez le champ API Key.'}}), 400
    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'Content-Type': 'application/json',
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01'
            },
            json=payload,
            timeout=60
        )
        text = resp.text.strip()
        if not text:
            return jsonify({'error': {'message': f"Réponse vide de l'API Anthropic (HTTP {resp.status_code})"}}), 502
        try:
            return jsonify(resp.json())
        except Exception:
            return jsonify({'error': {'message': f"Réponse non-JSON ({resp.status_code}): {text[:300]}"}}), 502
    except Exception as e:
        return jsonify({'error': {'message': str(e)}}), 500

# ── Fetch image (base64) ───────────────────────────────────────────────────
@app.route('/fetch_image', methods=['POST'])
def fetch_image():
    import io
    from PIL import Image
    data = request.json
    url = data.get('url', '')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    try:
        resp = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        if not resp.content:
            return jsonify({'error': 'Image vide'}), 204

        MAX_BYTES = 4 * 1024 * 1024  # 4MB pour rester sous la limite Anthropic de 5MB

        img_bytes = resp.content

        # Redimensionner si trop grande
        if len(img_bytes) > MAX_BYTES:
            img = Image.open(io.BytesIO(img_bytes))
            # Convertir en RGB si nécessaire (ex: PNG RGBA)
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            # Réduire progressivement jusqu'à passer sous la limite
            quality = 85
            scale = 1.0
            while True:
                buf = io.BytesIO()
                w = int(img.width * scale)
                h = int(img.height * scale)
                resized = img.resize((w, h), Image.LANCZOS) if scale < 1.0 else img
                resized.save(buf, format='JPEG', quality=quality, optimize=True)
                img_bytes = buf.getvalue()
                if len(img_bytes) <= MAX_BYTES:
                    break
                if quality > 50:
                    quality -= 10
                else:
                    scale *= 0.8
                if scale < 0.1:
                    break

        content_type = 'image/jpeg'
        b64 = base64.b64encode(img_bytes).decode('utf-8')
        return jsonify({'base64': b64, 'mediaType': content_type})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# ETSY OAuth2 PKCE
# ══════════════════════════════════════════════════════════════════════════════

def generate_pkce_pair():
    """Génère un code_verifier et son code_challenge SHA256 base64url."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode('utf-8')
    digest = hashlib.sha256(verifier.encode('utf-8')).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('utf-8')
    return verifier, challenge

# ── Étape 1 : générer l'URL d'autorisation Etsy ───────────────────────────
@app.route('/etsy/auth_url', methods=['POST'])
def etsy_auth_url():
    data = request.json
    api_key = data.get('api_key', '').strip()
    if not api_key:
        return jsonify({'error': 'Etsy API key manquante'}), 400

    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(16)

    # Stocker verifier + api_key pour le callback
    etsy_state_store[state] = {'verifier': verifier, 'api_key': api_key}

    redirect_uri = 'https://fleamarket-seo-modif-meta-description.onrender.com/etsy/callback'
    scopes = 'listings_r%20listings_w'

    url = (
        f'https://www.etsy.com/oauth/connect'
        f'?response_type=code'
        f'&client_id={api_key}'
        f'&redirect_uri={redirect_uri}'
        f'&scope={scopes}'
        f'&state={state}'
        f'&code_challenge={challenge}'
        f'&code_challenge_method=S256'
    )
    return jsonify({'auth_url': url})

# ── Étape 2 : callback Etsy → échange code contre token ───────────────────
@app.route('/etsy/callback')
def etsy_callback():
    code  = request.args.get('code', '')
    state = request.args.get('state', '')
    error = request.args.get('error', '')

    if error:
        return f"<script>window.opener.postMessage({{etsyError:'{error}'}}, '*'); window.close();</script>"

    stored = etsy_state_store.pop(state, None)
    if not stored:
        return "<script>window.opener.postMessage({etsyError:'State invalide ou expiré'}, '*'); window.close();</script>"

    api_key  = stored['api_key']
    verifier = stored['verifier']
    redirect_uri = 'https://fleamarket-seo-modif-meta-description.onrender.com/etsy/callback'

    try:
        resp = requests.post(
            'https://api.etsy.com/v3/public/oauth/token',
            data={
                'grant_type':    'authorization_code',
                'client_id':     api_key,
                'redirect_uri':  redirect_uri,
                'code':          code,
                'code_verifier': verifier,
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=15
        )
        token_data = resp.json()
        if 'access_token' not in token_data:
            return f"<script>window.opener.postMessage({{etsyError:'{json.dumps(token_data)}'}}, '*'); window.close();</script>"

        # Stocker les tokens en mémoire
        import time
        _etsy_data = {
            'access_token':  token_data['access_token'],
            'refresh_token': token_data.get('refresh_token', ''),
            'expires_at':    time.time() + token_data.get('expires_in', 3600) - 60,
            'api_key':       api_key
        }
        etsy_token_store['current'] = _etsy_data
        _save_token('etsy', _etsy_data)
        return '''<html><body><p style="font-family:monospace;padding:20px;color:green">✓ Etsy connecté !</p>
        <script>try{window.opener.postMessage({etsySuccess:true},'*');}catch(e){}
        setTimeout(function(){window.close();},1500);</script></body></html>'''
    except Exception as e:
        return f"<script>window.opener.postMessage({{etsyError:'{str(e)}'}}, '*'); window.close();</script>"

# ── Refresh token si expiré ────────────────────────────────────────────────
def get_valid_etsy_token():
    import time
    stored = etsy_token_store.get('current')
    if not stored:
        raise Exception('Non connecté à Etsy — cliquez sur "Connecter Etsy"')

    if time.time() > stored['expires_at']:
        # Refresh
        resp = requests.post(
            'https://api.etsy.com/v3/public/oauth/token',
            data={
                'grant_type':    'refresh_token',
                'client_id':     stored['api_key'],
                'refresh_token': stored['refresh_token'],
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=15
        )
        new_data = resp.json()
        if 'access_token' not in new_data:
            raise Exception(f"Refresh Etsy échoué: {new_data}")
        stored['access_token']  = new_data['access_token']
        stored['refresh_token'] = new_data.get('refresh_token', stored['refresh_token'])
        stored['expires_at']    = time.time() + new_data.get('expires_in', 3600) - 60
        etsy_token_store['current'] = stored

    return stored['access_token']

# ── Vérifier statut connexion Etsy ────────────────────────────────────────
@app.route('/etsy/status', methods=['GET'])
def etsy_status():
    stored = etsy_token_store.get('current')
    if stored:
        return jsonify({'connected': True})
    return jsonify({'connected': False})

# ── Mettre à jour un listing Etsy ─────────────────────────────────────────
@app.route('/etsy/update_listing', methods=['POST'])
def etsy_update_listing():
    data = request.json
    listing_id  = data.get('listing_id', '')
    title       = data.get('title', '')
    description = data.get('description', '')
    tags        = data.get('tags', [])  # liste de max 13 strings, max 20 car chacun

    if not listing_id:
        return jsonify({'error': 'listing_id manquant'}), 400

    try:
        token = get_valid_etsy_token()
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'x-api-key': etsy_token_store['current']['api_key']
        }

        # Nettoyer les tags : max 20 car, max 13 tags
        clean_tags = [t[:20] for t in tags if t.strip()][:13]

        payload = {}
        if title:       payload['title']       = title[:140]
        if description: payload['description'] = description
        if clean_tags:  payload['tags']         = clean_tags

        resp = requests.patch(
            f'https://openapi.etsy.com/v3/application/listings/{listing_id}',
            headers=headers,
            json=payload,
            timeout=30
        )

        if resp.status_code in (200, 201):
            return jsonify({'success': True, 'listing_id': listing_id})
        else:
            try:
                err = resp.json()
            except Exception:
                err = {'raw': resp.text[:300]}
            return jsonify({'error': err, 'status': resp.status_code}), resp.status_code

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Récupérer les listings Etsy actifs ────────────────────────────────────
@app.route('/etsy/listings', methods=['POST'])
def etsy_listings():
    data = request.json
    shop_id = data.get('shop_id', '')
    if not shop_id:
        return jsonify({'error': 'shop_id manquant'}), 400
    try:
        token = get_valid_etsy_token()
        api_key = etsy_token_store['current']['api_key']
        all_listings = []
        offset = 0
        limit = 100
        while True:
            resp = requests.get(
                f'https://openapi.etsy.com/v3/application/shops/{shop_id}/listings/active',
                headers={
                    'Authorization': f'Bearer {token}',
                    'x-api-key': api_key
                },
                params={'limit': limit, 'offset': offset},
                timeout=30
            )
            if resp.status_code != 200:
                return jsonify({'error': resp.text[:300], 'status': resp.status_code}), resp.status_code
            batch = resp.json()
            items = batch.get('results', [])
            all_listings.extend(items)
            if len(items) < limit:
                break
            offset += limit
        return jsonify({'listings': all_listings, 'count': len(all_listings)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



# ══════════════════════════════════════════════════════════════════════════════
# SHOPIFY OAuth
# ══════════════════════════════════════════════════════════════════════════════
SHOPIFY_CLIENT_ID     = 'a8c98892cc8e9f40ec51e6a64f9bdc1d'
SHOPIFY_CLIENT_SECRET = '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'
SHOPIFY_REDIRECT_URI  = 'https://fleamarket-seo-modif-meta-description.onrender.com/shopify/callback'
SHOPIFY_SCOPES        = 'read_products,write_products'
SHOPIFY_SHOP          = 'psangg-3f.myshopify.com'

shopify_state_store = {}   # state -> True (en mémoire, éphémère OK)

# Persistance tokens dans fichier pour survivre aux redémarrages Render
import os as _os

def _token_path(name):
    return _os.path.join('/tmp', 'fmf_' + name + '_token.json')

def _save_token(name, data):
    try:
        with open(_token_path(name), 'w') as _f:
            json.dump(data, _f)
    except Exception:
        pass

def _load_token(name):
    try:
        with open(_token_path(name), 'r') as _f:
            return json.load(_f)
    except Exception:
        return None

# Charger les tokens au démarrage
_shopify_tok = _load_token('shopify')
shopify_token_store = {'current': _shopify_tok} if _shopify_tok else {}

@app.route('/shopify/auth_url', methods=['POST'])
def shopify_auth_url():
    state = secrets.token_urlsafe(16)
    shopify_state_store[state] = True
    url = (
        'https://' + SHOPIFY_SHOP + '/admin/oauth/authorize'
        '?client_id=' + SHOPIFY_CLIENT_ID +
        '&scope=' + SHOPIFY_SCOPES +
        '&redirect_uri=' + SHOPIFY_REDIRECT_URI +
        '&state=' + state
    )
    return jsonify({'auth_url': url})

@app.route('/shopify/callback')
def shopify_callback():
    code  = request.args.get('code', '')
    state = request.args.get('state', '')
    error = request.args.get('error', '')

    if error:
        return "<script>window.opener.postMessage({shopifyError:'" + error + "'}, '*'); window.close();</script>"

    if state not in shopify_state_store:
        return "<script>window.opener.postMessage({shopifyError:'State invalide'}, '*'); window.close();</script>"

    shopify_state_store.pop(state, None)

    try:
        resp = requests.post(
            'https://' + SHOPIFY_SHOP + '/admin/oauth/access_token',
            json={
                'client_id':     SHOPIFY_CLIENT_ID,
                'client_secret': SHOPIFY_CLIENT_SECRET,
                'code':          code
            },
            headers={'Content-Type': 'application/json'},
            timeout=15
        )
        data = resp.json()
        if 'access_token' not in data:
            return "<script>window.opener.postMessage({shopifyError:'" + str(data) + "'}, '*'); window.close();</script>"

        shopify_token_store['current'] = data['access_token']
        _save_token('shopify', data['access_token'])
        return '''<html><body><p style="font-family:monospace;padding:20px;color:green">✓ Connecté !</p>
        <script>try{window.opener.postMessage({shopifySuccess:true},'*');}catch(e){}
        setTimeout(function(){window.close();},1500);</script></body></html>'''
    except Exception as e:
        err = str(e).replace("'", "")
        return '<html><body><p style="font-family:monospace;padding:20px;color:red">Erreur: ' + err + '</p><script>try{window.opener.postMessage({shopifyError:"' + err + '"},"*");}catch(e){}setTimeout(function(){window.close();},3000);</script></body></html>'

@app.route('/shopify/status', methods=['GET'])
def shopify_status():
    return jsonify({'connected': 'current' in shopify_token_store})

# ── Proxy Shopify API ──────────────────────────────────────────────────────
@app.route('/shopify/api', methods=['POST'])
def shopify_proxy():
    data   = request.json
    method = data.get('method', 'GET')
    path   = data.get('path', '')
    body   = data.get('body', None)

    token = shopify_token_store.get('current')
    if not token:
        return jsonify({'error': 'Non connecté à Shopify — cliquez sur "Connecter Shopify"'}), 401

    url = 'https://' + SHOPIFY_SHOP + '/admin/api/2024-01' + path
    try:
        resp = requests.request(
            method, url,
            headers={
                'X-Shopify-Access-Token': token,
                'Content-Type': 'application/json'
            },
            json=body,
            timeout=30
        )
        if not resp.content:
            return jsonify({'error': 'Réponse vide Shopify (HTTP ' + str(resp.status_code) + ')'}), 502
        try:
            json_data = resp.json()
            if resp.status_code >= 400:
                return jsonify({'error': str(json_data.get('errors', json_data))}), resp.status_code
            # Transmettre next_page_info si présent dans Link header
            link = resp.headers.get('Link', '')
            next_page = None
            if 'rel="next"' in link:
                import re as _re
                m = _re.search(r'page_info=([^&>]+).*?rel="next"', link)
                if m:
                    next_page = m.group(1)
            result = dict(json_data)
            if next_page:
                result['next_page_info'] = next_page
            return jsonify(result)
        except Exception:
            return jsonify({'error': 'Réponse non-JSON Shopify: ' + resp.text[:200]}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Proxy Etsy API (appels authentifiés) ──────────────────────────────────
@app.route('/etsy/api', methods=['POST'])
def etsy_api_proxy():
    data   = request.json
    method = data.get('method', 'GET')
    path   = data.get('path', '')
    body   = data.get('body', None)
    try:
        token   = get_valid_etsy_token()
        api_key = etsy_token_store['current']['api_key']
        resp = requests.request(
            method,
            'https://openapi.etsy.com/v3' + path,
            headers={
                'Authorization': 'Bearer ' + token,
                'Content-Type':  'application/json',
                'x-api-key':     api_key
            },
            json=body,
            timeout=30
        )
        if not resp.content:
            return jsonify({'error': 'Réponse vide Etsy (HTTP ' + str(resp.status_code) + ')'}), 502
        try:
            json_data = resp.json()
            if resp.status_code >= 400:
                return jsonify({'error': json_data}), resp.status_code
            return jsonify(json_data)
        except Exception:
            return jsonify({'error': 'Réponse non-JSON Etsy: ' + resp.text[:200]}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
