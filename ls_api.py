# -*- coding: utf-8 -*-
"""
Listing Studio - API complementaire (Blueprint Flask).

Routes :
  POST /ls/reserve          -> reserve N (sku + suffixe de titre jjmmaa+compteur)
  GET  /ls/registry_stats   -> etat du registre
  GET  /ls/ebay_profiles    -> profils d'expedition / paiement / retour eBay
  POST /ls/ebay_create      -> AddItem eBay (USD, site US, vend depuis la France)

Stockage durable : fichier JSON dans le depot GitHub, sur la branche `ls-data`
(branche separee pour ne PAS declencher de redeploiement Render a chaque ecriture).
Variables d'environnement Render requises :
  GITHUB_TOKEN     token GitHub (contents: read/write sur le depot)
  EBAY_USER_TOKEN  (optionnel) token eBay ; sinon repris de l'outil SEO via le navigateur
Optionnelles : GITHUB_REPO, LS_DATA_BRANCH, LS_DATA_PATH
"""
import base64
import json
import os
import random
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape

import requests
from flask import Blueprint, jsonify, request

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo('Europe/Paris')
except Exception:  # pragma: no cover
    _TZ = None

ls_bp = Blueprint('ls_api', __name__)

GH_REPO = os.environ.get('GITHUB_REPO', 'borntobuy/fleamarket-seo-modif-meta-description-titres')
GH_BRANCH = os.environ.get('LS_DATA_BRANCH', 'ls-data')
GH_PATH = os.environ.get('LS_DATA_PATH', 'ls_registry.json')
GH_API = 'https://api.github.com'

# Alphabet sans caracteres ambigus (pas de 0 O 1 I L)
SKU_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'
SKU_LEN = 8

_lock = threading.Lock()


# --------------------------------------------------------------------------
# Stockage GitHub
# --------------------------------------------------------------------------
class StoreError(Exception):
    pass


def _gh_headers():
    tok = os.environ.get('GITHUB_TOKEN', '').strip()
    if not tok:
        raise StoreError("GITHUB_TOKEN manquant sur Render (necessaire pour stocker SKU et compteur)")
    return {
        'Authorization': 'Bearer ' + tok,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }


def _ensure_branch(h):
    r = requests.get('%s/repos/%s/git/ref/heads/%s' % (GH_API, GH_REPO, GH_BRANCH), headers=h, timeout=20)
    if r.status_code == 200:
        return
    base = requests.get('%s/repos/%s/git/ref/heads/main' % (GH_API, GH_REPO), headers=h, timeout=20)
    if base.status_code != 200:
        raise StoreError('Branche main introuvable (%s)' % base.status_code)
    sha = base.json()['object']['sha']
    c = requests.post('%s/repos/%s/git/refs' % (GH_API, GH_REPO), headers=h,
                      json={'ref': 'refs/heads/' + GH_BRANCH, 'sha': sha}, timeout=20)
    if c.status_code not in (200, 201, 422):
        raise StoreError('Creation branche %s impossible (%s)' % (GH_BRANCH, c.status_code))


def _load(path=None):
    """Retourne (data, sha). sha=None si le fichier n'existe pas encore."""
    h = _gh_headers()
    r = requests.get('%s/repos/%s/contents/%s' % (GH_API, GH_REPO, path or GH_PATH),
                     headers=h, params={'ref': GH_BRANCH}, timeout=20)
    if r.status_code == 404:
        _ensure_branch(h)
        return ({'skus': {}, 'counters': {}} if not path else {}), None
    if r.status_code != 200:
        raise StoreError('Lecture registre impossible (%s)' % r.status_code)
    j = r.json()
    raw = base64.b64decode(j['content']).decode('utf-8')
    data = json.loads(raw) if raw.strip() else {}
    if not path:
        data.setdefault('skus', {})
        data.setdefault('counters', {})
    return data, j['sha']


def _save(data, sha, message, path=None):
    h = _gh_headers()
    body = {
        'message': message,
        'content': base64.b64encode(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).decode('ascii'),
        'branch': GH_BRANCH,
    }
    if sha:
        body['sha'] = sha
    r = requests.put('%s/repos/%s/contents/%s' % (GH_API, GH_REPO, path or GH_PATH), headers=h, json=body, timeout=40)
    if r.status_code in (200, 201):
        return True
    if r.status_code in (409, 422):
        return False  # conflit de version -> l'appelant relit et recommence
    raise StoreError('Ecriture registre impossible (%s)' % r.status_code)


def _today_key():
    now = datetime.now(_TZ) if _TZ else datetime.utcnow()
    return now.strftime('%d%m%y'), now.isoformat(timespec='seconds')


def _new_sku(existing):
    for _ in range(200):
        sku = 'FMF-' + ''.join(random.SystemRandom().choice(SKU_ALPHABET) for _ in range(SKU_LEN))
        if sku not in existing:
            return sku
    raise StoreError('Impossible de generer un SKU unique')


@ls_bp.route('/ls/reserve', methods=['POST'])
def ls_reserve():
    """Reserve N SKU + N suffixes de titre (jjmmaa + numero du jour)."""
    count = max(1, min(int((request.json or {}).get('count', 1)), 50))
    try:
        with _lock:
            for _attempt in range(6):
                data, sha = _load()
                day, iso = _today_key()
                n = int(data['counters'].get(day, 0))
                out = []
                for _ in range(count):
                    n += 1
                    sku = _new_sku(data['skus'])
                    suffix = '%s%d' % (day, n)
                    data['skus'][sku] = {'at': iso, 'suffix': suffix}
                    out.append({'sku': sku, 'suffix': suffix, 'n': n})
                data['counters'][day] = n
                if _save(data, sha, 'ls: reserve %d (%s)' % (count, day)):
                    return jsonify({'items': out})
            return jsonify({'error': 'Conflit de version du registre, reessayer'}), 409
    except StoreError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': 'registre: %s' % e}), 500


@ls_bp.route('/ls/registry_stats')
def ls_registry_stats():
    try:
        data, _ = _load()
        day, _iso = _today_key()
        return jsonify({'total_skus': len(data['skus']), 'today': day,
                        'today_count': data['counters'].get(day, 0)})
    except StoreError as e:
        return jsonify({'error': str(e)}), 500


# --------------------------------------------------------------------------
# eBay
# --------------------------------------------------------------------------
EBAY_URL = 'https://api.ebay.com/ws/api.dll'
EBAY_NS = '{urn:ebay:apis:eBLBaseComponents}'


def _creds():
    h = request.headers
    return {
        'token': (h.get('X-LS-EBAY-TOKEN') or os.environ.get('EBAY_USER_TOKEN', '')).strip(),
        'app': (h.get('X-LS-EBAY-APP') or os.environ.get('EBAY_APP_ID', '')).strip(),
        'cert': (h.get('X-LS-EBAY-CERT') or os.environ.get('EBAY_CERT_ID', '')).strip(),
    }


def _ebay_call(call_name, inner_xml):
    c = _creds()
    if not c['token']:
        raise StoreError("Token eBay manquant : clique sur le bouton Réglages en haut de la page et saisis-le une seule fois.")
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<%sRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
           '<RequesterCredentials><eBayAuthToken>%s</eBayAuthToken></RequesterCredentials>'
           '%s</%sRequest>') % (call_name, c['token'], inner_xml, call_name)
    headers = {
        'X-EBAY-API-SITEID': '0',
        'X-EBAY-API-COMPATIBILITY-LEVEL': '1193',
        'X-EBAY-API-CALL-NAME': call_name,
        'Content-Type': 'text/xml',
    }
    if c['app']:
        headers['X-EBAY-API-APP-NAME'] = c['app']
        headers['X-EBAY-API-DEV-NAME'] = ''
    if c['cert']:
        headers['X-EBAY-API-CERT-NAME'] = c['cert']
    resp = requests.post(EBAY_URL, headers=headers, data=xml.encode('utf-8'), timeout=40)
    body = (resp.text or '').strip()
    if not body.startswith('<'):
        raise StoreError('eBay %s : réponse vide ou illisible (HTTP %s) %s' % (call_name, resp.status_code, body[:120]))
    return ET.fromstring(resp.content)


# ---- API REST eBay (Taxonomy) : categories et caracteristiques --------------
_oauth_cache = {}


def _app_token():
    c = _creds()
    if not (c['app'] and c['cert']):
        raise StoreError("Identifiants eBay manquants : clique sur le bouton Réglages en haut de la page, saisis App ID, Cert ID et token eBay une seule fois, puis Enregistrer.")
    cached = _oauth_cache.get(c['app'])
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    basic = base64.b64encode(('%s:%s' % (c['app'], c['cert'])).encode()).decode()
    r = requests.post('https://api.ebay.com/identity/v1/oauth2/token',
                      headers={'Content-Type': 'application/x-www-form-urlencoded', 'Authorization': 'Basic ' + basic},
                      data={'grant_type': 'client_credentials', 'scope': 'https://api.ebay.com/oauth/api_scope'},
                      timeout=20)
    if r.status_code != 200:
        raise StoreError('OAuth eBay refusé (%s) : %s' % (r.status_code, r.text[:150]))
    j = r.json()
    _oauth_cache[c['app']] = (j['access_token'], time.time() + int(j.get('expires_in', 7200)))
    return j['access_token']


def _rest_get(url, params=None):
    r = requests.get(url, headers={'Authorization': 'Bearer ' + _app_token(), 'Accept': 'application/json',
                                   'Accept-Language': 'en-US', 'X-EBAY-C-MARKETPLACE-ID': 'EBAY_US'},
                     params=params, timeout=25)
    if r.status_code != 200:
        raise StoreError('eBay REST %s : %s' % (r.status_code, r.text[:200]))
    return r.json()


TAXO = 'https://api.ebay.com/commerce/taxonomy/v1/category_tree/0/'


def _taxo_suggest(q):
    j = _rest_get(TAXO + 'get_category_suggestions', {'q': q})
    out = []
    for cs in j.get('categorySuggestions', []):
        cat = cs.get('category') or {}
        anc = sorted(cs.get('categoryTreeNodeAncestors') or [], key=lambda a: a.get('categoryTreeNodeLevel', 0))
        names = [a.get('categoryName', '') for a in anc] + [cat.get('categoryName', '')]
        if cat.get('categoryId'):
            out.append({'id': str(cat['categoryId']), 'name': cat.get('categoryName', ''),
                        'path': ' > '.join(n for n in names if n), 'percent': ''})
    return out


def _t(node, path):
    el = node.find(path)
    return el.text if el is not None and el.text else ''


def _errors(root):
    errs = []
    for e in root.findall('.//%sErrors' % EBAY_NS):
        sev = _t(e, EBAY_NS + 'SeverityCode')
        msg = _t(e, EBAY_NS + 'LongMessage') or _t(e, EBAY_NS + 'ShortMessage')
        errs.append((sev, msg))
    return errs


@ls_bp.route('/ls/ebay_profiles')
def ls_ebay_profiles():
    try:
        root = _ebay_call('GetUserPreferences',
                          '<ShowSellerProfilePreferences>true</ShowSellerProfilePreferences>')
        out = {'shipping': [], 'payment': [], 'return': []}
        for p in root.iter(EBAY_NS + 'SupportedSellerProfile'):
            ptype = _t(p, EBAY_NS + 'ProfileType').upper()
            item = {
                'id': _t(p, EBAY_NS + 'ProfileID'),
                'name': _t(p, EBAY_NS + 'ProfileName'),
                'summary': _t(p, EBAY_NS + 'ShortSummary'),
            }
            if ptype == 'SHIPPING':
                out['shipping'].append(item)
            elif ptype == 'PAYMENT':
                out['payment'].append(item)
            elif 'RETURN' in ptype:
                out['return'].append(item)
        if not any(out.values()):
            errs = _errors(root)
            if errs:
                return jsonify({'error': errs[0][1]}), 502
        return jsonify(out)
    except StoreError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@ls_bp.route('/ls/ebay_categories')
def ls_ebay_categories():
    """Categories feuilles suggerees par eBay (Taxonomy API, site US) pour un titre."""
    q = (request.args.get('q') or '').strip()[:350]
    if not q:
        return jsonify({'categories': []})
    try:
        return jsonify({'categories': _taxo_suggest(q)[:8]})
    except StoreError as e:
        return jsonify({'error': str(e)}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500


_aspect_cache = {}


@ls_bp.route('/ls/ebay_aspects')
def ls_ebay_aspects():
    """Caracteristiques (requises/recommandees, valeurs autorisees) + etats valides d'une categorie."""
    cid = (request.args.get('category_id') or '').strip()
    if not cid.isdigit():
        return jsonify({'error': 'category_id invalide'}), 400
    if cid in _aspect_cache:
        return jsonify(_aspect_cache[cid])
    try:
        j = _rest_get(TAXO + 'get_item_aspects_for_category', {'category_id': cid})
        aspects = []
        for a in j.get('aspects', []):
            con = a.get('aspectConstraint') or {}
            usage = 'Required' if con.get('aspectRequired') else ('Recommended' if con.get('aspectUsage') == 'RECOMMENDED' else 'Optional')
            multi = con.get('itemToAspectCardinality') == 'MULTI'
            aspects.append({
                'name': a.get('localizedAspectName', ''),
                'usage': usage,
                'mode': 'SelectionOnly' if con.get('aspectMode') == 'SELECTION_ONLY' else 'FreeText',
                'max': 30 if multi else 1,
                'values': [v.get('localizedValue') for v in (a.get('aspectValues') or []) if v.get('localizedValue')][:40],
            })
        order = {'Required': 0, 'Recommended': 1, 'Optional': 2}
        aspects = [a for a in aspects if a['name']]
        aspects.sort(key=lambda a: order.get(a['usage'], 2))

        conditions = []
        try:
            m = _rest_get('https://api.ebay.com/sell/metadata/v1/marketplace/EBAY_US/get_item_condition_policies',
                          {'filter': 'categoryIds:{%s}' % cid})
            for pol in m.get('itemConditionPolicies', []):
                for c in pol.get('itemConditions', []):
                    conditions.append({'id': str(c.get('conditionId')), 'name': c.get('conditionDescription', '')})
        except Exception as e:
            print('[ls/ebay_aspects] conditions indisponibles:', e, file=sys.stderr)

        res = {'category_id': cid, 'aspects': aspects, 'conditions': conditions}
        if aspects:
            _aspect_cache[cid] = res
        return jsonify(res)
    except StoreError as e:
        return jsonify({'error': str(e)}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _suggest_category(title):
    sug = _taxo_suggest(title[:350])
    return sug[0]['id'] if sug else None


def _cdata(s):
    return '<![CDATA[' + (s or '').replace(']]>', ']]]]><![CDATA[>') + ']]>'


@ls_bp.route('/ls/ebay_create', methods=['POST'])
def ls_ebay_create():
    d = request.json or {}
    title = str(d.get('title', ''))[:80]
    description = str(d.get('description', ''))
    try:
        price = float(d.get('price') or 0)
    except Exception:
        price = 0
    if not title or price <= 0:
        return jsonify({'error': 'titre ou prix manquant'}), 400

    ship_id = str(d.get('shipping_profile_id') or '')
    pay_id = str(d.get('payment_profile_id') or '')
    ret_id = str(d.get('return_profile_id') or '')
    if not (ship_id and pay_id and ret_id):
        return jsonify({'error': "Choisis un profil d'expedition eBay (les profils paiement/retour sont pris automatiquement)"}), 400

    try:
        category_id = str(d.get('category_id') or '') or _suggest_category(title)
        if not category_id:
            return jsonify({'error': 'Categorie eBay introuvable pour ce titre'}), 502

        specifics = ''
        for k, v in (d.get('item_specifics') or {}).items():
            vals = [x for x in (v if isinstance(v, list) else [v]) if str(x or '').strip()]
            if k and vals:
                specifics += '<NameValueList><Name>%s</Name>%s</NameValueList>' % (
                    xml_escape(str(k)[:65]),
                    ''.join('<Value>%s</Value>' % xml_escape(str(x).strip()[:65]) for x in vals[:30]))
        specifics_xml = '<ItemSpecifics>%s</ItemSpecifics>' % specifics if specifics else ''

        pics = ''.join('<PictureURL>%s</PictureURL>' % xml_escape(u) for u in (d.get('image_urls') or [])[:24])
        pics_xml = '<PictureDetails>%s</PictureDetails>' % pics if pics else ''
        sku = str(d.get('sku') or '')
        sku_xml = '<SKU>%s</SKU>' % xml_escape(sku) if sku else ''

        item = (
            '<Item>'
            '<Title>%s</Title>'
            '<Description>%s</Description>'
            '<PrimaryCategory><CategoryID>%s</CategoryID></PrimaryCategory>'
            '<StartPrice currencyID="USD">%.2f</StartPrice>'
            '%s'
            '<Country>FR</Country><Currency>USD</Currency><Location>France</Location>'
            '<ListingDuration>GTC</ListingDuration><ListingType>FixedPriceItem</ListingType>'
            '<Quantity>1</Quantity>'
            '%s%s%s'
            '<SellerProfiles>'
            '<SellerShippingProfile><ShippingProfileID>%s</ShippingProfileID></SellerShippingProfile>'
            '<SellerPaymentProfile><PaymentProfileID>%s</PaymentProfileID></SellerPaymentProfile>'
            '<SellerReturnProfile><ReturnProfileID>%s</ReturnProfileID></SellerReturnProfile>'
            '</SellerProfiles>'
            '</Item>'
        ) % (xml_escape(title), _cdata(description), xml_escape(category_id), price,
             ('<ConditionID>%s</ConditionID>' % xml_escape(str(d['condition_id']))) if d.get('condition_id') else '',
             sku_xml, specifics_xml, pics_xml,
             xml_escape(ship_id), xml_escape(pay_id), xml_escape(ret_id))

        root = _ebay_call('AddFixedPriceItem', item)
        ack = _t(root, EBAY_NS + 'Ack')
        item_id = _t(root, EBAY_NS + 'ItemID')
        errs = _errors(root)
        print('[ls/ebay_create] ack=%s item=%s errs=%s' % (ack, item_id, errs[:3]), file=sys.stderr)
        if ack in ('Success', 'Warning') and item_id:
            res = {'success': True, 'item_id': item_id, 'ack': ack, 'category_id': category_id}
            warns = [m for s, m in errs if s == 'Warning']
            if warns:
                res['warnings'] = warns[:3]
            return jsonify(res)
        hard = [m for s, m in errs if s == 'Error'] or [m for s, m in errs]
        return jsonify({'error': hard[0] if hard else 'Erreur eBay', 'all': hard[:5]}), 502
    except StoreError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# --------------------------------------------------------------------------
# Connexions Etsy / Shopify qui survivent aux redemarrages de Render :
# le navigateur garde les jetons et les redonne au serveur.
# --------------------------------------------------------------------------
def _app_mod():
    m = sys.modules.get('app') or sys.modules.get('__main__')
    if not m or not hasattr(m, 'etsy_token_store') or not hasattr(m, '_save_token'):
        raise StoreError('module principal introuvable')
    return m


@ls_bp.route('/ls/restore', methods=['POST'])
def ls_restore():
    d = request.json or {}
    out = {}
    try:
        m = _app_mod()
        e = d.get('etsy')
        if e and e.get('access_token') and e.get('api_key'):
            cur = m.etsy_token_store.get('current')
            if d.get('force') or not cur or float(e.get('expires_at', 0)) > float(cur.get('expires_at', 0)):
                m.etsy_token_store['current'] = e
                m._save_token('etsy', e)
                out['etsy'] = 'restored'
            else:
                out['etsy'] = 'kept'
        sh = d.get('shopify')
        if sh and isinstance(sh, str) and not m.shopify_token_store.get('current'):
            m.shopify_token_store['current'] = sh
            m._save_token('shopify', sh)
            out['shopify'] = 'restored'
        return jsonify(out)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# --------------------------------------------------------------------------
# Connexions via variables d'environnement Render (plus aucune reconnexion) :
#   SHOPIFY_ACCESS_TOKEN                       -> Shopify
#   ETSY_API_KEY, ETSY_SHARED_SECRET, ETSY_REFRESH_TOKEN -> Etsy
#   EBAY_APP_ID, EBAY_CERT_ID, EBAY_USER_TOKEN -> eBay (deja gere par _creds)
# --------------------------------------------------------------------------
_env_lock = threading.Lock()
_env_state = {'etsy_try': 0.0}


def _env_bootstrap():
    try:
        m = _app_mod()
    except Exception:
        return
    sh = os.environ.get('SHOPIFY_ACCESS_TOKEN', '').strip()
    if sh and m.shopify_token_store.get('current') != sh:
        m.shopify_token_store['current'] = sh
    key = os.environ.get('ETSY_API_KEY', '').strip()
    rt = os.environ.get('ETSY_REFRESH_TOKEN', '').strip()
    if not (key and rt):
        return
    cur = m.etsy_token_store.get('current')
    if cur and cur.get('api_key') == key and float(cur.get('expires_at', 0)) > time.time() + 120:
        return
    if time.time() - _env_state['etsy_try'] < 30:
        return
    with _env_lock:
        _env_state['etsy_try'] = time.time()
        secret = os.environ.get('ETSY_SHARED_SECRET', '').strip()
        # reprend le dernier refresh_token renouvele s'il existe (meme cle)
        use_rt = rt
        if cur and cur.get('api_key') == key and cur.get('refresh_token'):
            use_rt = cur['refresh_token']
        for cand in ([use_rt, rt] if use_rt != rt else [rt]):
            try:
                r = requests.post('https://api.etsy.com/v3/public/oauth/token',
                                  data={'grant_type': 'refresh_token', 'client_id': key, 'refresh_token': cand},
                                  headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=15)
                j = r.json()
            except Exception:
                continue
            if 'access_token' in j:
                data = {'access_token': j['access_token'],
                        'refresh_token': j.get('refresh_token', cand),
                        'expires_at': time.time() + int(j.get('expires_in', 3600)) - 60,
                        'api_key': key, 'secret': secret}
                m.etsy_token_store['current'] = data
                try:
                    m._save_token('etsy', data)
                except Exception:
                    pass
                return


@ls_bp.before_app_request
def _ls_env_before():
    _env_bootstrap()


@ls_bp.after_app_request
def _ls_callback_inject(resp):
    """Les fenetres OAuth perdent souvent window.opener (politique COOP de Shopify/Etsy).
    La page de retour est sur notre domaine : elle ecrit donc elle-meme les jetons dans le
    localStorage du navigateur, et la page Listing Studio les voit via l'evenement 'storage'."""
    try:
        if request.path not in ('/shopify/callback', '/etsy/callback') or resp.status_code != 200:
            return resp
        if 'html' not in (resp.mimetype or ''):
            return resp
        m = _app_mod()
        if request.path == '/shopify/callback':
            tok = m.shopify_token_store.get('current')
            if not tok:
                return resp
            js = "localStorage.setItem('ls_token_shopify'," + json.dumps(tok) + ");"
        else:
            cur = m.etsy_token_store.get('current')
            if not cur:
                return resp
            js = "localStorage.setItem('ls_tokens_etsy'," + json.dumps(json.dumps(cur)) + ");"
        js = js.replace('</', '<\\/')
        body = resp.get_data(as_text=True)
        resp.set_data(body + '<script>try{' + js + '}catch(e){}</script>')
    except Exception:
        pass
    return resp


@ls_bp.route('/ls/env_status', methods=['GET'])
def ls_env_status():
    _env_bootstrap()
    e = os.environ.get
    return jsonify({
        'ebay': bool(e('EBAY_APP_ID') and e('EBAY_CERT_ID') and e('EBAY_USER_TOKEN')),
        'etsy': bool(e('ETSY_API_KEY') and e('ETSY_SHARED_SECRET') and e('ETSY_REFRESH_TOKEN')),
        'shopify': bool(e('SHOPIFY_ACCESS_TOKEN')),
        'github': bool(e('GITHUB_TOKEN')),
    })


@ls_bp.route('/ls/etsy_refresh', methods=['POST'])
def ls_etsy_refresh():
    d = request.json or {}
    if not d.get('api_key') or not d.get('refresh_token'):
        return jsonify({'error': 'api_key ou refresh_token manquant'}), 400
    try:
        r = requests.post('https://api.etsy.com/v3/public/oauth/token',
                          data={'grant_type': 'refresh_token', 'client_id': d['api_key'],
                                'refresh_token': d['refresh_token']},
                          headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=15)
        j = r.json()
        if 'access_token' not in j:
            return jsonify({'error': 'Refresh Etsy refusé : %s' % str(j)[:150]}), 400
        return jsonify({'access_token': j['access_token'],
                        'refresh_token': j.get('refresh_token', d['refresh_token']),
                        'expires_at': time.time() + int(j.get('expires_in', 3600)) - 60,
                        'api_key': d['api_key'], 'secret': d.get('secret', '')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# --------------------------------------------------------------------------
# STOCK : liste unifiee + synchronisation des ventes (controle toutes les heures)
#   GET  /ls/stock_sync?dry=1   -> vue en direct (n'ecrit rien, n'agit pas)
#   GET|POST /ls/stock_sync     -> synchronisation reelle (cron GitHub Actions)
#   GET  /ls/stock              -> derniere liste enregistree + dernier controle
# Regle : un article actif qui disparait d'une plateforme (vendu / termine) est
# desactive sur les autres. Premier passage = simple etat des lieux, sans action.
# --------------------------------------------------------------------------
PLATS = ('ebay', 'etsy', 'shopify')
STOCK_PATH = os.environ.get('LS_STOCK_PATH', 'ls_stock.json')
MAX_AUTO = 10            # au-dela, on n'agit pas (protection contre une panne d'API)
_sync_lock = threading.Lock()


def _norm_title(t):
    t = re.sub(r'\s+', ' ', str(t or '').lower()).strip()
    t = re.sub(r'\s*\b\d{6,8}$', '', t)  # retire la reference jjmmaa+n
    return t[:40]


def _ebay_active():
    out = []
    page = 1
    while True:
        root = _ebay_call('GetMyeBaySelling',
                          '<ActiveList><Include>true</Include><Pagination><EntriesPerPage>200</EntriesPerPage>'
                          '<PageNumber>%d</PageNumber></Pagination></ActiveList>' % page)
        errs = [e for e in _errors(root) if e[0] == 'Error']
        if errs:
            raise StoreError('eBay : ' + errs[0][1][:150])
        for it in root.findall('.//%sActiveList/%sItemArray/%sItem' % (EBAY_NS, EBAY_NS, EBAY_NS)):
            out.append({
                'id': _t(it, EBAY_NS + 'ItemID'),
                'title': _t(it, EBAY_NS + 'Title'),
                'sku': _t(it, EBAY_NS + 'SKU').strip(),
                'price': _t(it, EBAY_NS + 'SellingStatus/' + EBAY_NS + 'CurrentPrice'),
                'url': _t(it, EBAY_NS + 'ListingDetails/' + EBAY_NS + 'ViewItemURL'),
            })
        total = int(_t(root, './/%sActiveList/%sPaginationResult/%sTotalNumberOfPages' % (EBAY_NS, EBAY_NS, EBAY_NS)) or 1)
        if page >= total or page >= 30:
            break
        page += 1
    return out


def _etsy_ctx():
    m = _app_mod()
    _env_bootstrap()
    cur = m.etsy_token_store.get('current')
    if not cur:
        raise StoreError('Etsy non connecte')
    if float(cur.get('expires_at', 0)) < time.time() + 60 and cur.get('refresh_token'):
        r = requests.post('https://api.etsy.com/v3/public/oauth/token',
                          data={'grant_type': 'refresh_token', 'client_id': cur['api_key'],
                                'refresh_token': cur['refresh_token']},
                          headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=15)
        j = r.json()
        if 'access_token' not in j:
            raise StoreError('Etsy : jeton expire, reconnecte Etsy (%s)' % str(j)[:100])
        cur = dict(cur, access_token=j['access_token'], refresh_token=j.get('refresh_token', cur['refresh_token']),
                   expires_at=time.time() + int(j.get('expires_in', 3600)) - 60)
        m.etsy_token_store['current'] = cur
        try:
            m._save_token('etsy', cur)
        except Exception:
            pass
    key = cur['api_key'] + (':' + cur['secret'] if cur.get('secret') else '')
    return {'Authorization': 'Bearer ' + cur['access_token'], 'x-api-key': key}


def _etsy_get(path, params=None):
    r = requests.get('https://openapi.etsy.com/v3' + path, headers=_etsy_ctx(), params=params, timeout=30)
    if r.status_code != 200:
        raise StoreError('Etsy %s : %s' % (r.status_code, r.text[:150]))
    return r.json()


def _etsy_active():
    me = _etsy_get('/application/users/me')
    shop = me.get('shop_id')
    if not shop:
        raise StoreError('Boutique Etsy introuvable')
    out, offset = [], 0
    while True:
        j = _etsy_get('/application/shops/%s/listings' % shop, {'state': 'active', 'limit': 100, 'offset': offset})
        res = j.get('results', [])
        for it in res:
            pr = it.get('price') or {}
            price = ''
            if pr.get('amount') is not None:
                price = '%.2f' % (pr['amount'] / (pr.get('divisor') or 100))
            skus = it.get('skus') or []
            out.append({'id': str(it.get('listing_id')), 'title': it.get('title', ''),
                        'sku': (skus[0] if skus else '').strip(), 'price': price, 'url': it.get('url', ''),
                        'shop': shop})
        offset += 100
        if not res or offset >= int(j.get('count', 0)) or offset > 5000:
            break
    return out


def _shopify_ctx():
    m = _app_mod()
    _env_bootstrap()
    tok = m.shopify_token_store.get('current')
    if not tok:
        raise StoreError('Shopify non connecte')
    return m.SHOPIFY_SHOP, {'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'}


def _shopify_active():
    shop, h = _shopify_ctx()
    out = []
    url = 'https://%s/admin/api/2024-01/products.json' % shop
    params = {'status': 'active', 'limit': 250, 'fields': 'id,title,handle,variants'}
    for _ in range(40):
        r = requests.get(url, headers=h, params=params, timeout=40)
        if r.status_code != 200:
            raise StoreError('Shopify %s : %s' % (r.status_code, r.text[:150]))
        for p in r.json().get('products', []):
            vs = p.get('variants') or []
            sku = next((v.get('sku') for v in vs if v.get('sku')), '') or ''
            out.append({'id': str(p['id']), 'title': p.get('title', ''), 'sku': sku.strip(),
                        'price': (vs[0].get('price') if vs else '') or '',
                        'url': 'https://%s/products/%s' % (shop, p.get('handle', ''))})
        nxt = (r.links or {}).get('next', {}).get('url')
        if not nxt:
            break
        url, params = nxt, None
    return out


def _deactivate(plat, ref):
    """Desactive un article sur une plateforme. Retourne un texte de resultat."""
    if plat == 'etsy':
        h = _etsy_ctx()
        h['Content-Type'] = 'application/json'
        r = requests.patch('https://openapi.etsy.com/v3/application/shops/%s/listings/%s' % (ref['shop'], ref['id']),
                           headers=h, json={'state': 'inactive'}, timeout=30)
        if r.status_code not in (200, 201):
            raise StoreError('Etsy %s : %s' % (r.status_code, r.text[:120]))
        return 'Etsy -> inactif'
    if plat == 'shopify':
        shop, h = _shopify_ctx()
        r = requests.put('https://%s/admin/api/2024-01/products/%s.json' % (shop, ref['id']), headers=h,
                         json={'product': {'id': int(ref['id']), 'status': 'draft'}}, timeout=30)
        if r.status_code != 200:
            raise StoreError('Shopify %s : %s' % (r.status_code, r.text[:120]))
        return 'Shopify -> brouillon'
    if plat == 'ebay':
        root = _ebay_call('EndFixedPriceItem',
                          '<ItemID>%s</ItemID><EndingReason>NotAvailable</EndingReason>' % xml_escape(ref['id']))
        errs = [e for e in _errors(root) if e[0] == 'Error']
        if errs:
            raise StoreError('eBay : ' + errs[0][1][:120])
        return 'eBay -> termine'
    raise StoreError('plateforme inconnue')


def _merge(current):
    """current = {plat: [items]} -> liste d'articles fusionnes par SKU (sinon titre)."""
    merged = []
    by_sku, by_t = {}, {}
    for plat in PLATS:
        for it in current.get(plat, []):
            tk = _norm_title(it['title'])
            ent = (by_sku.get(it['sku']) if it['sku'] else None) or by_t.get(tk)
            if ent is None or plat in ent['p']:
                ent = {'sku': it['sku'], 'tkey': tk, 'title': it['title'], 'price': it['price'], 'p': {}}
                merged.append(ent)
            ent['p'][plat] = {k: it[k] for k in ('id', 'url', 'shop') if k in it}
            if it['sku'] and not ent['sku']:
                ent['sku'] = it['sku']
            if it['sku']:
                by_sku[it['sku']] = ent
            by_t[tk] = ent
            if plat == 'ebay':
                ent['title'] = it['title']
                ent['price'] = it['price'] or ent['price']
    return merged


def _fetch_all():
    current, errors = {}, {}
    for plat, fn in (('ebay', _ebay_active), ('etsy', _etsy_active), ('shopify', _shopify_active)):
        try:
            current[plat] = fn()
        except Exception as e:  # une plateforme en panne ne bloque pas les autres
            errors[plat] = str(e)[:200]
    return current, errors


def _view(merged, status_by_key=None):
    rows = []
    for e in merged:
        rows.append({'sku': e['sku'], 'title': e['title'], 'price': e['price'],
                     'platforms': {p: e['p'][p].get('url', '') or True for p in e['p']}})
    return rows


@ls_bp.route('/ls/stock')
def ls_stock():
    try:
        st, _ = _load(STOCK_PATH)
        items = list((st.get('items') or {}).values())
        return jsonify({'items': items, 'last': st.get('last'), 'errors': st.get('errors', {}),
                        'log': (st.get('log') or [])[-30:]})
    except StoreError as e:
        return jsonify({'error': str(e)}), 500


@ls_bp.route('/ls/stock_sync', methods=['GET', 'POST'])
def ls_stock_sync():
    key = os.environ.get('STOCK_CRON_KEY', '').strip()
    dry = request.args.get('dry') == '1'
    if key and not dry and request.args.get('key') != key:
        return jsonify({'error': 'cle invalide'}), 403
    if not _sync_lock.acquire(blocking=False):
        return jsonify({'error': 'synchronisation deja en cours'}), 409
    try:
        current, errors = _fetch_all()
        merged = _merge(current)
        if dry:
            return jsonify({'dry': True, 'items': _view(merged), 'errors': errors,
                            'counts': {p: len(current.get(p, [])) for p in PLATS}})

        st, sha = _load(STOCK_PATH)
        items = st.get('items') or {}
        now = _today_key()[1]
        ok_plats = [p for p in PLATS if p in current]
        # garde-fou : une plateforme qui renvoie 0 alors qu'on en connaissait beaucoup = panne probable
        for p in list(ok_plats):
            known = sum(1 for i in items.values() if i.get('status') == 'active' and (i['p'].get(p) or {}).get('active'))
            if known >= 5 and not current[p]:
                errors[p] = 'liste vide alors que %d articles etaient connus : ignore' % known
                ok_plats.remove(p)

        idx_sku = {i['sku']: k for k, i in items.items() if i.get('sku')}
        idx_t = {i['tkey']: k for k, i in items.items() if i.get('tkey')}
        seen_keys = set()
        for e in merged:
            k = idx_sku.get(e['sku']) if e['sku'] else None
            k = k or idx_t.get(e['tkey'])
            if not k:
                k = e['sku'] or ('t:' + e['tkey'])
                items[k] = {'key': k, 'sku': e['sku'], 'tkey': e['tkey'], 'first': now, 'p': {}, 'status': 'active'}
            it = items[k]
            seen_keys.add(k)
            it.update({'title': e['title'], 'price': e['price']})
            if e['sku'] and not it.get('sku'):
                it['sku'] = e['sku']
            for p, ref in e['p'].items():
                if it['status'] == 'sold' and not (it['p'].get(p) or {}).get('active'):
                    it['status'] = 'active'  # remis en vente
                it['p'][p] = dict(ref, active=True)

        first_run = not st.get('last')
        actions, blocked, newly_sold = [], [], []
        for k, it in items.items():
            if it.get('status') != 'active' or not it.get('sku'):
                continue  # sans SKU : rapprochement par titre trop risque, jamais d'action auto
            gone = [p for p in ok_plats if (it['p'].get(p) or {}).get('active') and
                    p not in next((m['p'] for m in merged if (m['sku'] and m['sku'] == it.get('sku')) or m['tkey'] == it.get('tkey')), {})]
            if not gone:
                continue
            for p in gone:
                it['p'][p]['active'] = False
            newly_sold.append((k, gone))

        if len(newly_sold) > MAX_AUTO and not first_run:
            blocked = [{'key': k, 'title': items[k].get('title'), 'gone_on': g} for k, g in newly_sold]
            for k, g in newly_sold:  # on annule le marquage pour reessayer a la prochaine fois
                for p in g:
                    items[k]['p'][p]['active'] = True
            newly_sold = []
        for k, gone in newly_sold:
            items[k]['pending'] = sorted(set(items[k].get('pending') or []) | set(gone))
        for k, it in items.items():
            gone = it.get('pending')
            if not gone or it.get('status') != 'active':
                continue
            failed = False
            for q in PLATS:
                ref = it['p'].get(q) or {}
                if q in ok_plats and ref.get('active') and q not in gone:
                    try:
                        res = _deactivate(q, ref)
                        ref['active'] = False
                        actions.append({'title': it.get('title'), 'sku': it.get('sku'), 'gone_on': gone, 'did': res})
                    except Exception as ex:
                        failed = True
                        actions.append({'title': it.get('title'), 'sku': it.get('sku'), 'gone_on': gone,
                                        'error': '%s : %s' % (q, str(ex)[:120])})
                elif ref.get('active') and q not in gone:
                    failed = True  # plateforme injoignable ce coup-ci : on reessaie plus tard
            if not failed:
                it.pop('pending', None)
                it['status'] = 'sold'
                it['sold_on'] = gone
                it['sold_at'] = now
                it['sold_ts'] = time.time()

        # allegement : pas d'URL stockee, on oublie les vendus de plus de 90 jours
        for it in items.values():
            it.pop('tkey', None) if it.get('sku') else None
            for ref in it['p'].values():
                ref.pop('url', None)
        cutoff = time.time() - 90 * 86400
        for k in [k for k, i in items.items() if i.get('status') == 'sold' and i.get('sold_ts', 0) and i['sold_ts'] < cutoff]:
            del items[k]
        st['items'] = items
        st['last'] = now
        st['errors'] = errors
        log = st.get('log') or []
        for a in actions:
            log.append(dict(a, at=now))
        st['log'] = log[-200:]
        saved = False
        for _ in range(5):
            if _save(st, sha, 'ls: stock sync %s' % now, STOCK_PATH):
                saved = True
                break
            _fresh, sha = _load(STOCK_PATH)
        return jsonify({'ok': saved, 'first_run': first_run, 'checked': {p: len(current.get(p, [])) for p in PLATS},
                        'actions': actions, 'blocked': blocked, 'errors': errors})
    except StoreError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': 'stock: %s' % e}), 500
    finally:
        _sync_lock.release()
