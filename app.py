from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import hashlib
import base64
import os
import secrets
import json
import time

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

# ── Persistance tokens (fichiers /tmp) ─────────────────────────────────────
def _token_path(name):
    return os.path.join('/tmp', 'fmf_' + name + '_token.json')

def _save_token(name, data):
    try:
        with open(_token_path(name), 'w') as f:
            json.dump(data, f)
    except Exception:
        pass

def _load_token(name):
    try:
        with open(_token_path(name), 'r') as f:
            return json.load(f)
    except Exception:
        return None

# ── Stores en mémoire (chargés depuis fichier au démarrage) ────────────────
_etsy_tok     = _load_token('etsy')
etsy_token_store  = {'current': _etsy_tok} if _etsy_tok else {}
etsy_state_store  = {}

_shopify_tok  = _load_token('shopify')
shopify_token_store = {'current': _shopify_tok} if _shopify_tok else {}
shopify_state_store = {}

# ── Page principale ────────────────────────────────────────────────────────
@app.route('/')
def index():
    return app.send_static_file('index.html')

# ── Proxy eBay XML ─────────────────────────────────────────────────────────
@app.route('/ebay', methods=['POST'])
def ebay_proxy():
    data = request.json
    headers = data.get('headers', {})
    body    = data.get('body', '')
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

# ── Proxy Anthropic ────────────────────────────────────────────────────────
@app.route('/anthropic', methods=['POST'])
def anthropic_proxy():
    data    = request.json
    api_key = data.get('api_key', '').strip()
    payload = data.get('payload', {})
    if not api_key:
        return jsonify({'error': {'message': 'Clé Anthropic manquante.'}}), 400
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
            return jsonify({'error': {'message': 'Réponse vide Anthropic (HTTP ' + str(resp.status_code) + ')'}}), 502
        try:
            return jsonify(resp.json())
        except Exception:
            return jsonify({'error': {'message': 'Réponse non-JSON (' + str(resp.status_code) + '): ' + text[:300]}}), 502
    except Exception as e:
        return jsonify({'error': {'message': str(e)}}), 500

# ── Fetch image base64 ─────────────────────────────────────────────────────
@app.route('/fetch_image', methods=['POST'])
def fetch_image():
    import io
    try:
        from PIL import Image
        pil_available = True
    except ImportError:
        pil_available = False

    data = request.json
    url  = data.get('url', '')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    try:
        resp = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        if not resp.content:
            return jsonify({'error': 'Image vide'}), 204

        MAX_BYTES = 3 * 1024 * 1024
        img_bytes = resp.content

        if pil_available and len(img_bytes) > MAX_BYTES:
            img = Image.open(io.BytesIO(img_bytes))
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            quality = 85
            scale   = 1.0
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

        # Garder le type original si pas de compression, sinon JPEG
        orig_type = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
        content_type = 'image/jpeg' if (pil_available and len(resp.content) > MAX_BYTES) else orig_type
        # S'assurer que le type est accepté par Anthropic (jpeg, png, gif, webp)
        if content_type not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp'):
            content_type = 'image/jpeg'
        b64 = base64.b64encode(img_bytes).decode('utf-8')
        return jsonify({'base64': b64, 'mediaType': content_type})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ══════════════════════════════════════════════════════════════════════════════
# ETSY OAuth2 PKCE
# ══════════════════════════════════════════════════════════════════════════════
def generate_pkce_pair():
    verifier  = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode('utf-8')
    digest    = hashlib.sha256(verifier.encode('utf-8')).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('utf-8')
    return verifier, challenge

@app.route('/etsy/auth_url', methods=['POST'])
def etsy_auth_url():
    data    = request.json
    api_key = data.get('api_key', '').strip()
    if not api_key:
        return jsonify({'error': 'Etsy API key manquante'}), 400
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(16)
    etsy_state_store[state] = {'verifier': verifier, 'api_key': api_key}
    redirect_uri = 'https://fleamarket-seo-modif-meta-description.onrender.com/etsy/callback'
    url = (
        'https://www.etsy.com/oauth/connect'
        '?response_type=code'
        '&client_id=' + api_key +
        '&redirect_uri=' + redirect_uri +
        '&scope=listings_r%20listings_w'
        '&state=' + state +
        '&code_challenge=' + challenge +
        '&code_challenge_method=S256'
    )
    return jsonify({'auth_url': url})

@app.route('/etsy/callback')
def etsy_callback():
    code  = request.args.get('code', '')
    state = request.args.get('state', '')
    error = request.args.get('error', '')
    if error:
        return '<html><body><p style="font-family:monospace;padding:20px;color:red">Erreur: ' + error + '</p><script>try{window.opener.postMessage({etsyError:"' + error + '"},"*");}catch(e){}setTimeout(function(){window.close();},3000);</script></body></html>'
    stored = etsy_state_store.pop(state, None)
    if not stored:
        return '<html><body><p style="font-family:monospace;padding:20px;color:red">State invalide</p><script>try{window.opener.postMessage({etsyError:"State invalide"},"*");}catch(e){}setTimeout(function(){window.close();},3000);</script></body></html>'
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
            err = str(token_data)
            return '<html><body><p style="font-family:monospace;padding:20px;color:red">Erreur token: ' + err + '</p><script>try{window.opener.postMessage({etsyError:"' + err[:100] + '"},"*");}catch(e){}setTimeout(function(){window.close();},3000);</script></body></html>'
        etsy_data = {
            'access_token':  token_data['access_token'],
            'refresh_token': token_data.get('refresh_token', ''),
            'expires_at':    time.time() + token_data.get('expires_in', 3600) - 60,
            'api_key':       api_key
        }
        etsy_token_store['current'] = etsy_data
        _save_token('etsy', etsy_data)
        return '<html><body><p style="font-family:monospace;padding:20px;color:green">✓ Etsy connecté !</p><script>try{window.opener.postMessage({etsySuccess:true},"*");}catch(e){}setTimeout(function(){window.close();},1500);</script></body></html>'
    except Exception as e:
        err = str(e)
        return '<html><body><p style="font-family:monospace;padding:20px;color:red">Erreur: ' + err + '</p><script>try{window.opener.postMessage({etsyError:"' + err[:100] + '"},"*");}catch(e){}setTimeout(function(){window.close();},3000);</script></body></html>'

def get_valid_etsy_token():
    stored = etsy_token_store.get('current')
    if not stored:
        raise Exception('Non connecté à Etsy')
    if time.time() > stored.get('expires_at', 0):
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
            raise Exception('Refresh Etsy échoué: ' + str(new_data))
        stored['access_token']  = new_data['access_token']
        stored['refresh_token'] = new_data.get('refresh_token', stored['refresh_token'])
        stored['expires_at']    = time.time() + new_data.get('expires_in', 3600) - 60
        etsy_token_store['current'] = stored
        _save_token('etsy', stored)
    return stored['access_token']

@app.route('/etsy/status', methods=['GET'])
def etsy_status():
    return jsonify({'connected': 'current' in etsy_token_store})

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

@app.route('/etsy/update_listing', methods=['POST'])
def etsy_update_listing():
    data        = request.json
    listing_id  = data.get('listing_id', '')
    title       = data.get('title', '')
    description = data.get('description', '')
    tags        = data.get('tags', [])
    if not listing_id:
        return jsonify({'error': 'listing_id manquant'}), 400
    try:
        token   = get_valid_etsy_token()
        api_key = etsy_token_store['current']['api_key']
        clean_tags = [t[:20] for t in tags if t.strip()][:13]
        payload = {}
        if title:       payload['title']       = title[:140]
        if description: payload['description'] = description
        if clean_tags:  payload['tags']         = clean_tags
        resp = requests.patch(
            'https://openapi.etsy.com/v3/application/listings/' + listing_id,
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'x-api-key': api_key},
            json=payload,
            timeout=30
        )
        if resp.status_code in (200, 201):
            return jsonify({'success': True})
        else:
            try:
                err = resp.json()
            except Exception:
                err = {'raw': resp.text[:300]}
            return jsonify({'error': err, 'status': resp.status_code}), resp.status_code
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
        return '<html><body><p style="font-family:monospace;padding:20px;color:red">Erreur: ' + error + '</p><script>try{window.opener.postMessage({shopifyError:"' + error + '"},"*");}catch(e){}setTimeout(function(){window.close();},3000);</script></body></html>'
    if state not in shopify_state_store:
        return '<html><body><p style="font-family:monospace;padding:20px;color:red">State invalide</p><script>try{window.opener.postMessage({shopifyError:"State invalide"},"*");}catch(e){}setTimeout(function(){window.close();},3000);</script></body></html>'
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
            err = str(data)
            return '<html><body><p style="font-family:monospace;padding:20px;color:red">Erreur token: ' + err + '</p><script>try{window.opener.postMessage({shopifyError:"' + err[:100] + '"},"*");}catch(e){}setTimeout(function(){window.close();},3000);</script></body></html>'
        shopify_token_store['current'] = data['access_token']
        _save_token('shopify', data['access_token'])
        return '<html><body><p style="font-family:monospace;padding:20px;color:green">✓ Shopify connecté !</p><script>try{window.opener.postMessage({shopifySuccess:true},"*");}catch(e){}setTimeout(function(){window.close();},1500);</script></body></html>'
    except Exception as e:
        err = str(e)
        return '<html><body><p style="font-family:monospace;padding:20px;color:red">Erreur: ' + err + '</p><script>try{window.opener.postMessage({shopifyError:"' + err[:100] + '"},"*");}catch(e){}setTimeout(function(){window.close();},3000);</script></body></html>'

@app.route('/shopify/status', methods=['GET'])
def shopify_status():
    return jsonify({'connected': 'current' in shopify_token_store})


SHOPIFY_MARKER = '<!-- fmf-shopify -->'

@app.route('/shopify/products', methods=['POST'])
def shopify_get_products():
    import re as _re
    data      = request.json
    force_all = data.get('force_all', False)
    token     = shopify_token_store.get('current')
    if not token:
        return jsonify({'error': 'Non connecte a Shopify'}), 401

    all_products = []
    url = 'https://' + SHOPIFY_SHOP + '/admin/api/2024-01/products.json?limit=250&fields=id,title,variants,metafields_global_title_tag,metafields_global_description_tag,handle,body_html,images'

    # Récupérer le total via count
    try:
        count_resp = requests.get(
            'https://' + SHOPIFY_SHOP + '/admin/api/2024-01/products/count.json',
            headers={'X-Shopify-Access-Token': token}, timeout=15
        )
        total_count = count_resp.json().get('count', 0) if count_resp.status_code == 200 else 0
    except Exception:
        total_count = 0

    page_num = 0
    try:
        while url:
            page_num += 1
            resp = requests.get(url, headers={'X-Shopify-Access-Token': token}, timeout=90)
            if resp.status_code != 200:
                return jsonify({'error': 'Shopify HTTP ' + str(resp.status_code) + ': ' + resp.text[:200]}), resp.status_code
            products = resp.json().get('products', [])
            for p in products:
                seo_title    = p.get('metafields_global_title_tag') or ''
                seo_desc     = p.get('metafields_global_description_tag') or ''
                body_html  = p.get('body_html') or ''
                seo_title_ = p.get('metafields_global_title_tag') or ''
                already_done = SHOPIFY_MARKER in body_html or SHOPIFY_MARKER in seo_title_
                if not force_all and already_done:
                    continue
                sku   = p['variants'][0].get('sku', '')   if p.get('variants') else ''
                price = p['variants'][0].get('price', '') if p.get('variants') else ''
                all_products.append({
                    'id': p['id'], 'title': p.get('title', ''), 'handle': p.get('handle', ''),
                    'sku': sku, 'price': price, 'seoTitle': seo_title, 'seoDesc': seo_desc,
                    'hasSeo': bool(seo_title and seo_desc), 'alreadyDone': already_done,
                    'bodyHtml': (p.get('body_html') or ''),
                    'images':  [img.get('src','') for img in (p.get('images') or [])]
                })
            url  = None
            link = resp.headers.get('Link', '')
            if 'rel="next"' in link:
                m = _re.search(r'<([^>]+)>; *rel="next"', link)
                if m:
                    url = m.group(1)

        return jsonify({'products': all_products, 'total': len(all_products), 'total_store': total_count, 'pages': page_num})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/shopify/update_seo', methods=['POST'])
def shopify_update_seo():
    data       = request.json
    product_id = data.get('product_id')
    seo_title  = data.get('seoTitle', '')
    seo_desc   = data.get('seoDesc', '')
    token      = shopify_token_store.get('current')
    if not token:
        return jsonify({'error': 'Non connecte a Shopify'}), 401
    try:
        marked_title = seo_title  # Le marqueur va UNIQUEMENT dans body_html, pas dans le SEO title
        description = data.get('description', '')
        handle = data.get('handle', '')
        payload = {'product': {
            'id': product_id,
            'metafields_global_title_tag':       marked_title,
            'metafields_global_description_tag': seo_desc
        }}
        if description:
            payload['product']['body_html'] = description + '\n' + SHOPIFY_MARKER
        if handle:
            payload['product']['handle'] = handle
        resp = requests.put(
            'https://' + SHOPIFY_SHOP + '/admin/api/2024-01/products/' + str(product_id) + '.json',
            headers={'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'},
            json=payload,
            timeout=30
        )
        if resp.status_code not in (200, 201):
            return jsonify({'error': resp.text[:300]}), resp.status_code
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/shopify/api', methods=['POST'])
def shopify_proxy():
    data   = request.json
    method = data.get('method', 'GET')
    path   = data.get('path', '')
    body   = data.get('body', None)
    token  = shopify_token_store.get('current')
    if not token:
        return jsonify({'error': 'Non connecté à Shopify'}), 401
    url = 'https://' + SHOPIFY_SHOP + '/admin/api/2024-01' + path
    try:
        resp = requests.request(
            method, url,
            headers={'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'},
            json=body,
            timeout=30
        )
        # Log détaillé pour debug
        import sys
        print('SHOPIFY DEBUG: status=' + str(resp.status_code) + ' url=' + url, file=sys.stderr)
        print('SHOPIFY DEBUG: response=' + resp.text[:300], file=sys.stderr)

        if not resp.content:
            return jsonify({'error': 'Réponse vide Shopify (HTTP ' + str(resp.status_code) + ')'}), 502
        try:
            json_data = resp.json()
            if resp.status_code >= 400:
                return jsonify({'error': str(json_data.get('errors', json_data))}), resp.status_code
            import re as _re
            link      = resp.headers.get('Link', '')
            next_page = None
            if 'rel="next"' in link:
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

@app.route('/shopify/cleanup_markers', methods=['POST'])
def shopify_cleanup_markers():
    token = shopify_token_store.get('current')
    if not token:
        return jsonify({'error': 'Non connecte a Shopify'}), 401
    import sys, re as _re, time as _time
    # Récupérer l'offset depuis la requête pour reprendre où on s'est arrêté
    data      = request.json or {}
    offset    = data.get('offset', 0)
    fixed     = data.get('fixed', 0)
    errors    = data.get('errors', [])
    BATCH     = 15  # produits par batch pour rester sous le timeout

    try:
        # Récupérer tous les IDs en une fois (rapide)
        all_ids = []
        url = 'https://' + SHOPIFY_SHOP + '/admin/api/2024-01/products.json?limit=250&fields=id,body_html'
        all_products = {}
        while url:
            resp = requests.get(url, headers={'X-Shopify-Access-Token': token}, timeout=30)
            for p in resp.json().get('products', []):
                all_products[p['id']] = p.get('body_html') or ''
            url = None
            link = resp.headers.get('Link', '')
            if 'rel="next"' in link:
                m = _re.search(r'<([^>]+)>; *rel="next"', link)
                if m: url = m.group(1)

        all_ids   = list(all_products.keys())
        total     = len(all_ids)
        batch_ids = all_ids[offset:offset + BATCH]

        print('CLEANUP: total=' + str(total) + ' offset=' + str(offset) + ' batch=' + str(len(batch_ids)), file=sys.stderr)

        for pid in batch_ids:
            mf_resp = requests.get(
                'https://' + SHOPIFY_SHOP + '/admin/api/2024-01/products/' + str(pid) + '/metafields.json',
                headers={'X-Shopify-Access-Token': token}, timeout=15
            )
            metafields = mf_resp.json().get('metafields', [])
            seo_mf = next((m for m in metafields if m.get('key') == 'title_tag'), None)
            if not seo_mf:
                continue
            seo_title = seo_mf.get('value', '')
            if SHOPIFY_MARKER not in seo_title:
                continue

            clean_title = seo_title.replace(SHOPIFY_MARKER, '').strip()
            patch = requests.put(
                'https://' + SHOPIFY_SHOP + '/admin/api/2024-01/metafields/' + str(seo_mf['id']) + '.json',
                headers={'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'},
                json={'metafield': {'id': seo_mf['id'], 'value': clean_title, 'type': 'single_line_text_field'}},
                timeout=15
            )
            if patch.status_code in (200, 201):
                fixed += 1
                body_html = all_products[pid]
                if SHOPIFY_MARKER not in body_html:
                    requests.put(
                        'https://' + SHOPIFY_SHOP + '/admin/api/2024-01/products/' + str(pid) + '.json',
                        headers={'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'},
                        json={'product': {'id': pid, 'body_html': body_html + '\n' + SHOPIFY_MARKER}},
                        timeout=15
                    )
            else:
                errors.append(str(pid))

        next_offset = offset + BATCH
        done        = next_offset >= total

        return jsonify({
            'fixed':       fixed,
            'errors':      errors,
            'offset':      next_offset,
            'total':       total,
            'done':        done,
            'progress':    min(next_offset, total)
        })
    except Exception as e:
        import traceback
        print('CLEANUP ERROR: ' + traceback.format_exc(), file=sys.stderr)
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
