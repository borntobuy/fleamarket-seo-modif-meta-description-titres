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


def _ebay_page(page):
    """Une page d'annonces eBay actives et achetables -> (items, nb_pages)."""
    out = []
    root = _ebay_call('GetMyeBaySelling',
                      '<ActiveList><Include>true</Include><Pagination><EntriesPerPage>200</EntriesPerPage>'
                      '<PageNumber>%d</PageNumber></Pagination></ActiveList>' % page)
    errs = [e for e in _errors(root) if e[0] == 'Error']
    if errs:
        raise StoreError('eBay : ' + errs[0][1][:150])
    for it in root.findall('.//%sActiveList/%sItemArray/%sItem' % (EBAY_NS, EBAY_NS, EBAY_NS)):
        qa = _t(it, EBAY_NS + 'QuantityAvailable')
        if qa == '':
            q, s = _t(it, EBAY_NS + 'Quantity'), _t(it, EBAY_NS + 'SellingStatus/' + EBAY_NS + 'QuantitySold')
            qa = str(max(int(q or 1) - int(s or 0), 0)) if q else ''
        qty = int(qa) if qa != '' else None
        if qty is not None and qty <= 0:
            continue  # plus achetable
        out.append({
            'id': _t(it, EBAY_NS + 'ItemID'),
            'title': _t(it, EBAY_NS + 'Title'),
            'sku': _t(it, EBAY_NS + 'SKU').strip(),
            'price': _t(it, EBAY_NS + 'SellingStatus/' + EBAY_NS + 'CurrentPrice'),
            'url': _t(it, EBAY_NS + 'ListingDetails/' + EBAY_NS + 'ViewItemURL'),
            'img': _t(it, EBAY_NS + 'PictureDetails/' + EBAY_NS + 'GalleryURL'),
            'qty': qty,
        })
    total = int(_t(root, './/%sActiveList/%sPaginationResult/%sTotalNumberOfPages' % (EBAY_NS, EBAY_NS, EBAY_NS)) or 1)
    return out, total


def _ebay_active():
    out, page = [], 1
    while True:
        items, total = _ebay_page(page)
        out += items
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


_etsy_shop_cache = {}


def _etsy_shop():
    if not _etsy_shop_cache.get('id'):
        shop = _etsy_get('/application/users/me').get('shop_id')
        if not shop:
            raise StoreError('Boutique Etsy introuvable')
        _etsy_shop_cache['id'] = shop
    return _etsy_shop_cache['id']


def _etsy_page(offset):
    """Une page (100) d'annonces Etsy actives et achetables -> (items, count_total)."""
    shop = _etsy_shop()
    j = _etsy_get('/application/shops/%s/listings' % shop, {'state': 'active', 'limit': 100, 'offset': offset, 'includes': 'Images'})
    out = []
    for it in j.get('results', []):
        qty = it.get('quantity')
        if qty is not None and int(qty) <= 0:
            continue
        pr = it.get('price') or {}
        price = ''
        if pr.get('amount') is not None:
            price = '%.2f' % (pr['amount'] / (pr.get('divisor') or 100))
        skus = it.get('skus') or []
        out.append({'id': str(it.get('listing_id')), 'title': it.get('title', ''),
                    'sku': (skus[0] if skus else '').strip(), 'price': price, 'url': it.get('url', ''),
                    'shop': shop, 'qty': qty,
                    'img': ((it.get('images') or [{}])[0].get('url_75x75') or '')})
    return out, int(j.get('count', 0))


def _etsy_active():
    out, offset = [], 0
    while True:
        items, count = _etsy_page(offset)
        out += items
        offset += 100
        if offset >= count or offset > 5000:
            break
    return out


def _shopify_ctx():
    m = _app_mod()
    _env_bootstrap()
    tok = m.shopify_token_store.get('current')
    if not tok:
        raise StoreError('Shopify non connecte')
    return m.SHOPIFY_SHOP, {'X-Shopify-Access-Token': tok, 'Content-Type': 'application/json'}


def _shopify_count():
    shop, h = _shopify_ctx()
    r = requests.get('https://%s/admin/api/2024-01/products/count.json' % shop, headers=h,
                     params={'status': 'active', 'published_status': 'published'}, timeout=30)
    return int(r.json().get('count', 0)) if r.status_code == 200 else 0


def _shopify_page(cursor=None):
    """Une page (250) de produits Shopify actifs, publies et achetables -> (items, curseur_suivant)."""
    shop, h = _shopify_ctx()
    url = 'https://%s/admin/api/2024-01/products.json' % shop
    fields = 'id,title,handle,variants,image'
    if cursor:
        params = {'limit': 250, 'page_info': cursor, 'fields': fields}
    else:
        params = {'status': 'active', 'published_status': 'published', 'limit': 250, 'fields': fields}
    r = requests.get(url, headers=h, params=params, timeout=40)
    if r.status_code != 200:
        raise StoreError('Shopify %s : %s' % (r.status_code, r.text[:150]))
    out = []
    for p in r.json().get('products', []):
        vs = p.get('variants') or []
        qty, buyable = 0, False
        for v in vs:
            tracked = (v.get('inventory_management') or '') != ''
            q = int(v.get('inventory_quantity') or 0)
            if not tracked or v.get('inventory_policy') == 'continue':
                buyable = True
            elif q > 0:
                buyable = True
            qty += max(q, 0)
        if vs and not buyable:
            continue  # brouillon impossible ici (filtre), mais stock a 0 = plus achetable
        sku = next((v.get('sku') for v in vs if v.get('sku')), '') or ''
        out.append({'id': str(p['id']), 'title': p.get('title', ''), 'sku': sku.strip(),
                    'price': (vs[0].get('price') if vs else '') or '',
                    'url': 'https://%s/products/%s' % (shop, p.get('handle', '')),
                    'img': ((p.get('image') or {}).get('src') or ''), 'qty': qty})
    nxt = (r.links or {}).get('next', {}).get('url') or ''
    token = ''
    if nxt:
        from urllib.parse import urlparse, parse_qs
        token = (parse_qs(urlparse(nxt).query).get('page_info') or [''])[0]
    return out, token


def _shopify_active():
    out, cur = [], None
    for _ in range(40):
        items, cur = _shopify_page(cur)
        out += items
        if not cur:
            break
    return out


@ls_bp.route('/ls/stock_part')
def ls_stock_part():
    """Une page d'une plateforme (le navigateur boucle et affiche le pourcentage d'avancement)."""
    plat = request.args.get('plat', '')
    try:
        if plat == 'ebay':
            page = int(request.args.get('page', 1))
            items, total = _ebay_page(page)
            return jsonify({'items': items, 'pages': total})
        if plat == 'etsy':
            offset = int(request.args.get('offset', 0))
            items, count = _etsy_page(offset)
            return jsonify({'items': items, 'pages': max(1, -(-count // 100)), 'next': offset + 100 if offset + 100 < count else None})
        if plat == 'shopify':
            cur = request.args.get('cursor') or None
            items, nxt = _shopify_page(cur)
            out = {'items': items, 'next': nxt or None}
            if not cur:
                out['pages'] = max(1, -(-_shopify_count() // 250))
            return jsonify(out)
        return jsonify({'error': 'plateforme inconnue'}), 400
    except Exception as e:
        return jsonify({'error': str(e)[:200]}), 500


@ls_bp.route('/ls/stock_merge', methods=['POST'])
def ls_stock_merge():
    cur = (request.json or {}).get('current') or {}
    merged = _merge({p: cur.get(p) or [] for p in PLATS})
    return jsonify({'items': _view(merged)})


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


def _stock_left(plat, ref):
    """(nb_variantes, quantite_dispo_totale) d'une annonce, ou None si illisible."""
    try:
        if plat == 'ebay':
            root = _ebay_call('GetItem', '<ItemID>%s</ItemID><DetailLevel>ReturnAll</DetailLevel>' % xml_escape(ref['id']))
            it = root.find('.//%sItem' % EBAY_NS)
            if it is None:
                return None
            vs = it.findall('.//%sVariations/%sVariation' % (EBAY_NS, EBAY_NS))
            if vs:
                tot = 0
                for v in vs:
                    q = int(_t(v, EBAY_NS + 'Quantity') or 0)
                    s = int(_t(v, EBAY_NS + 'SellingStatus/' + EBAY_NS + 'QuantitySold') or 0)
                    tot += max(q - s, 0)
                return len(vs), tot
            q = int(_t(it, EBAY_NS + 'Quantity') or 0)
            s = int(_t(it, EBAY_NS + 'SellingStatus/' + EBAY_NS + 'QuantitySold') or 0)
            return 1, max(q - s, 0)
        if plat == 'etsy':
            j = _etsy_get('/application/listings/%s/inventory' % ref['id'])
            ps = j.get('products') or []
            tot = 0
            for p in ps:
                for o in p.get('offerings') or []:
                    if o.get('is_enabled', True) and not o.get('is_deleted'):
                        tot += int(o.get('quantity') or 0)
            return max(len(ps), 1), tot
        if plat == 'shopify':
            shop, h = _shopify_ctx()
            r = requests.get('https://%s/admin/api/2024-01/products/%s.json' % (shop, ref['id']), headers=h,
                             params={'fields': 'variants'}, timeout=30)
            if r.status_code != 200:
                return None
            vs = r.json().get('product', {}).get('variants') or []
            return len(vs), sum(max(int(v.get('inventory_quantity') or 0), 0) for v in vs)
    except Exception:
        return None
    return None


def _merge(current):
    """current = {plat: [items]} -> liste d'articles fusionnes par SKU (sinon titre)."""
    merged = []
    by_sku, by_t = {}, {}
    for plat in PLATS:
        for it in current.get(plat, []):
            tk = _norm_title(it['title'])
            ent = (by_sku.get(it['sku']) if it['sku'] else None) or by_t.get(tk)
            if ent is None or plat in ent['p']:
                ent = {'sku': it['sku'], 'tkey': tk, 'title': it['title'], 'price': it['price'], 'p': {}, 'img': ''}
                merged.append(ent)
            ent['p'][plat] = {k: it[k] for k in ('id', 'url', 'shop') if k in it}
            if it.get('img') and (plat == 'ebay' or not ent['img']):
                ent['img'] = it['img']
            ent.setdefault('price_by', {})[plat] = it['price']
            ent.setdefault('qty_by', {})[plat] = it.get('qty')
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
        rows.append({'sku': e['sku'], 'title': e['title'], 'price': e['price'], 'img': e.get('img', ''),
                     'price_by': e.get('price_by', {}), 'qty_by': e.get('qty_by', {}),
                     'platforms': {p: e['p'][p].get('url', '') or True for p in e['p']},
                     'refs': {p: {k: e['p'][p][k] for k in ('id', 'shop') if k in e['p'][p]} for p in e['p']}})
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
            if e.get('img'):
                it['img'] = e['img']
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
            # annonce a variantes / quantite > 1 : on ne desactive que si plus aucun stock
            keep = False
            for q in PLATS:
                ref = it['p'].get(q) or {}
                if q in ok_plats and ref.get('active') and q not in gone:
                    sl = _stock_left(q, ref)
                    if sl is None:
                        keep = True  # doute : on ne touche a rien
                    elif sl[1] > 0 and (sl[0] > 1 or sl[1] > 1):
                        keep = True
            if keep:
                it.pop('pending', None)
                for q in gone:
                    if (it['p'].get(q) or {}).get('active') is False:
                        it['p'][q]['active'] = False
                actions.append({'title': it.get('title'), 'sku': it.get('sku'), 'gone_on': gone,
                                'did': 'ignore : stock restant sur une variante'})
                continue
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


# --------------------------------------------------------------------------
# STOCK : actions groupees (desactiver, prix), lecture d'une annonce source,
# proxy d'images pour l'export vers une autre plateforme
# --------------------------------------------------------------------------
def _set_price(plat, ref, price):
    price = round(float(price), 2)
    if price <= 0:
        raise StoreError('prix invalide')
    if plat == 'ebay':
        root = _ebay_call('ReviseInventoryStatus',
                          '<InventoryStatus><ItemID>%s</ItemID><StartPrice>%.2f</StartPrice></InventoryStatus>'
                          % (xml_escape(ref['id']), price))
        errs = [e for e in _errors(root) if e[0] == 'Error']
        if errs:
            raise StoreError('eBay : ' + errs[0][1][:120])
        return 'eBay %.2f' % price
    if plat == 'shopify':
        shop, h = _shopify_ctx()
        r = requests.get('https://%s/admin/api/2024-01/products/%s.json' % (shop, ref['id']),
                         headers=h, params={'fields': 'id,variants'}, timeout=30)
        if r.status_code != 200:
            raise StoreError('Shopify %s : %s' % (r.status_code, r.text[:100]))
        for v in r.json().get('product', {}).get('variants', []):
            u = requests.put('https://%s/admin/api/2024-01/variants/%s.json' % (shop, v['id']), headers=h,
                             json={'variant': {'id': v['id'], 'price': '%.2f' % price}}, timeout=30)
            if u.status_code != 200:
                raise StoreError('Shopify %s : %s' % (u.status_code, u.text[:100]))
        return 'Shopify %.2f' % price
    if plat == 'etsy':
        h = _etsy_ctx()
        r = requests.get('https://openapi.etsy.com/v3/application/listings/%s/inventory' % ref['id'], headers=h, timeout=30)
        if r.status_code != 200:
            raise StoreError('Etsy %s : %s' % (r.status_code, r.text[:100]))
        inv = r.json()
        products = []
        for pr in inv.get('products', []):
            offs = []
            for o in pr.get('offerings', []):
                offs.append({'quantity': o.get('quantity', 1), 'is_enabled': o.get('is_enabled', True), 'price': price})
            products.append({'sku': pr.get('sku', ''),
                             'property_values': [{'property_id': pv.get('property_id'), 'property_name': pv.get('property_name'),
                                                  'scale_id': pv.get('scale_id'), 'value_ids': pv.get('value_ids', []),
                                                  'values': pv.get('values', [])} for pv in pr.get('property_values', [])],
                             'offerings': offs})
        body = {'products': products,
                'price_on_property': inv.get('price_on_property') or [],
                'quantity_on_property': inv.get('quantity_on_property') or [],
                'sku_on_property': inv.get('sku_on_property') or []}
        h2 = dict(h)
        h2['Content-Type'] = 'application/json'
        u = requests.put('https://openapi.etsy.com/v3/application/listings/%s/inventory' % ref['id'],
                         headers=h2, json=body, timeout=30)
        if u.status_code not in (200, 201):
            raise StoreError('Etsy %s : %s' % (u.status_code, u.text[:100]))
        return 'Etsy %.2f' % price
    raise StoreError('plateforme inconnue')


@ls_bp.route('/ls/stock_action', methods=['POST'])
def ls_stock_action():
    """{action: 'deactivate'|'price', platforms: [...], items: [{title, refs:{plat:{id,shop}}, price}]}"""
    d = request.json or {}
    action = d.get('action')
    plats = [p for p in (d.get('platforms') or []) if p in PLATS]
    items = d.get('items') or []
    if action not in ('deactivate', 'price') or not plats or not items:
        return jsonify({'error': 'parametres invalides'}), 400
    if len(items) > 200:
        return jsonify({'error': 'maximum 200 articles a la fois'}), 400
    results = []
    for it in items:
        for p in plats:
            ref = (it.get('refs') or {}).get(p)
            if not ref:
                continue
            row = {'title': it.get('title', ''), 'platform': p}
            try:
                if action == 'deactivate':
                    row['ok'] = _deactivate(p, ref)
                else:
                    row['ok'] = _set_price(p, ref, it.get('price'))
            except Exception as e:
                row['error'] = str(e)[:150]
            results.append(row)
    return jsonify({'results': results})


@ls_bp.route('/ls/sku_used', methods=['POST'])
def ls_sku_used():
    """True si ce SKU appartient a un article deja vendu/desactive (relisting -> il faut un nouveau SKU)."""
    sku = str((request.json or {}).get('sku') or '').strip()
    if not sku:
        return jsonify({'used': False})
    try:
        st, _ = _load(STOCK_PATH)
        for it in (st.get('items') or {}).values():
            if it.get('sku') == sku and (it.get('status') == 'sold' or it.get('sold_at')):
                return jsonify({'used': True})
        return jsonify({'used': False})
    except StoreError as e:
        return jsonify({'error': str(e)}), 500


@ls_bp.route('/ls/ebay_set_sku', methods=['POST'])
def ls_ebay_set_sku():
    j = request.json or {}
    item_id, sku = str(j.get('item_id') or '').strip(), str(j.get('sku') or '').strip()
    if not item_id or not sku:
        return jsonify({'error': 'parametres manquants'}), 400
    try:
        root = _ebay_call('ReviseFixedPriceItem',
                          '<Item><ItemID>%s</ItemID><SKU>%s</SKU></Item>' % (xml_escape(item_id), xml_escape(sku)))
        errs = [e for e in _errors(root) if e[0] == 'Error']
        if errs:
            return jsonify({'error': 'eBay : ' + errs[0][1][:150]}), 400
        return jsonify({'ok': True})
    except StoreError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': 'eBay : %s' % e}), 500


@ls_bp.route('/ls/stock_source', methods=['POST'])
def ls_stock_source():
    """Lit une annonce existante (eBay > Shopify > Etsy) : titre, description, prix, photos, tags."""
    refs = (request.json or {}).get('refs') or {}
    out = {}
    try:
        if refs.get('ebay'):
            root = _ebay_call('GetItem', '<ItemID>%s</ItemID><DetailLevel>ReturnAll</DetailLevel>' % xml_escape(refs['ebay']['id']))
            it = root.find('.//%sItem' % EBAY_NS)
            if it is not None:
                out['ebay'] = {
                    'title': _t(it, EBAY_NS + 'Title'), 'description': _t(it, EBAY_NS + 'Description'),
                    'price': _t(it, EBAY_NS + 'SellingStatus/' + EBAY_NS + 'CurrentPrice') or _t(it, EBAY_NS + 'StartPrice'),
                    'sku': _t(it, EBAY_NS + 'SKU'),
                    'images': [e.text for e in it.findall('.//%sPictureDetails/%sPictureURL' % (EBAY_NS, EBAY_NS)) if e.text],
                }
    except Exception as e:
        out['ebay_error'] = str(e)[:120]
    try:
        if refs.get('shopify'):
            shop, h = _shopify_ctx()
            r = requests.get('https://%s/admin/api/2024-01/products/%s.json' % (shop, refs['shopify']['id']), headers=h, timeout=30)
            if r.status_code == 200:
                pr = r.json().get('product', {})
                vs = pr.get('variants') or [{}]
                out['shopify'] = {'title': pr.get('title', ''), 'description': pr.get('body_html', ''),
                                  'price': vs[0].get('price', ''), 'sku': vs[0].get('sku', ''),
                                  'images': [i.get('src') for i in pr.get('images', []) if i.get('src')],
                                  'tags': [t.strip() for t in (pr.get('tags') or '').split(',') if t.strip()]}
    except Exception as e:
        out['shopify_error'] = str(e)[:120]
    try:
        if refs.get('etsy'):
            j = _etsy_get('/application/listings/%s' % refs['etsy']['id'], {'includes': 'Images'})
            pr = j.get('price') or {}
            out['etsy'] = {'title': j.get('title', ''), 'description': j.get('description', ''),
                           'price': ('%.2f' % (pr['amount'] / (pr.get('divisor') or 100))) if pr.get('amount') is not None else '',
                           'sku': (j.get('skus') or [''])[0],
                           'images': [i.get('url_fullxfull') for i in j.get('images', []) if i.get('url_fullxfull')],
                           'tags': j.get('tags') or [], 'materials': j.get('materials') or []}
    except Exception as e:
        out['etsy_error'] = str(e)[:120]
    return jsonify(out)


_IMG_OK = re.compile(r'^https://([a-z0-9-]+\.)*(ebayimg\.com|etsystatic\.com|shopify\.com|shopifycdn\.com)/', re.I)


@ls_bp.route('/ls/imgproxy')
def ls_imgproxy():
    from flask import Response
    url = request.args.get('url', '')
    if not _IMG_OK.match(url):
        return jsonify({'error': 'hote non autorise'}), 400
    try:
        r = requests.get(url, timeout=25)
        if r.status_code != 200 or len(r.content) > 12 * 1024 * 1024:
            return jsonify({'error': 'image indisponible'}), 502
        return Response(r.content, mimetype=r.headers.get('Content-Type', 'image/jpeg'),
                        headers={'Cache-Control': 'public, max-age=3600'})
    except Exception as e:
        return jsonify({'error': str(e)[:100]}), 502


# --------------------------------------------------------------------------
# Etsy : envoi des photos puis mise en ligne (brouillon -> actif)
# --------------------------------------------------------------------------
@ls_bp.route('/ls/etsy_finish', methods=['POST'])
def ls_etsy_finish():
    """{shop_id, listing_id, images:[{data(base64 jpeg), alt}], activate:true}"""
    d = request.json or {}
    shop, lid = d.get('shop_id'), d.get('listing_id')
    images = (d.get('images') or [])[:10]
    if not shop or not lid:
        return jsonify({'error': 'shop_id / listing_id manquant'}), 400
    out = {'uploaded': 0, 'errors': []}
    try:
        h = _etsy_ctx()
        for i, im in enumerate(images):
            try:
                raw = base64.b64decode(im.get('data', ''))
                r = requests.post('https://openapi.etsy.com/v3/application/shops/%s/listings/%s/images' % (shop, lid),
                                  headers=h, files={'image': ('photo%d.jpg' % (i + 1), raw, 'image/jpeg')},
                                  data={'rank': i + 1, 'alt_text': str(im.get('alt') or '')[:500], 'overwrite': 'false'}, timeout=60)
                if r.status_code in (200, 201):
                    out['uploaded'] += 1
                else:
                    out['errors'].append('photo %d : %s %s' % (i + 1, r.status_code, r.text[:100]))
            except Exception as e:
                out['errors'].append('photo %d : %s' % (i + 1, str(e)[:80]))
        out['activated'] = False
        if d.get('activate', True):
            if out['uploaded'] == 0:
                out['errors'].append("aucune photo envoyee : annonce laissee en brouillon (Etsy exige au moins une photo)")
            else:
                h2 = dict(h)
                h2['Content-Type'] = 'application/json'
                r = requests.patch('https://openapi.etsy.com/v3/application/shops/%s/listings/%s' % (shop, lid),
                                   headers=h2, json={'state': 'active'}, timeout=40)
                if r.status_code in (200, 201):
                    out['activated'] = True
                else:
                    out['errors'].append('mise en ligne : %s %s' % (r.status_code, r.text[:160]))
        return jsonify(out)
    except StoreError as e:
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500
