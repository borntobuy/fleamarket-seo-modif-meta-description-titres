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
  EBAY_USER_TOKEN  token utilisateur eBay (deja utilise)
Optionnelles : GITHUB_REPO, LS_DATA_BRANCH, LS_DATA_PATH
"""
import base64
import json
import os
import random
import re
import sys
import threading
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


def _load():
    """Retourne (data, sha). sha=None si le fichier n'existe pas encore."""
    h = _gh_headers()
    r = requests.get('%s/repos/%s/contents/%s' % (GH_API, GH_REPO, GH_PATH),
                     headers=h, params={'ref': GH_BRANCH}, timeout=20)
    if r.status_code == 404:
        _ensure_branch(h)
        return {'skus': {}, 'counters': {}}, None
    if r.status_code != 200:
        raise StoreError('Lecture registre impossible (%s)' % r.status_code)
    j = r.json()
    raw = base64.b64decode(j['content']).decode('utf-8')
    data = json.loads(raw) if raw.strip() else {}
    data.setdefault('skus', {})
    data.setdefault('counters', {})
    return data, j['sha']


def _save(data, sha, message):
    h = _gh_headers()
    body = {
        'message': message,
        'content': base64.b64encode(json.dumps(data, ensure_ascii=False, indent=0).encode('utf-8')).decode('ascii'),
        'branch': GH_BRANCH,
    }
    if sha:
        body['sha'] = sha
    r = requests.put('%s/repos/%s/contents/%s' % (GH_API, GH_REPO, GH_PATH), headers=h, json=body, timeout=25)
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


def _ebay_call(call_name, inner_xml):
    token = os.environ.get('EBAY_USER_TOKEN', '').strip()
    if not token:
        raise StoreError('EBAY_USER_TOKEN manquant sur Render')
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<%sRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
           '<RequesterCredentials><eBayAuthToken>%s</eBayAuthToken></RequesterCredentials>'
           '%s</%sRequest>') % (call_name, token, inner_xml, call_name)
    resp = requests.post(EBAY_URL, headers={
        'X-EBAY-API-SITEID': '0',
        'X-EBAY-API-COMPATIBILITY-LEVEL': '1193',
        'X-EBAY-API-CALL-NAME': call_name,
        'Content-Type': 'text/xml',
    }, data=xml.encode('utf-8'), timeout=40)
    return ET.fromstring(resp.content)


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


def _suggest_category(title):
    root = _ebay_call('GetSuggestedCategories', '<Query>%s</Query>' % xml_escape(title[:350]))
    for c in root.iter(EBAY_NS + 'SuggestedCategory'):
        cid = _t(c, EBAY_NS + 'Category/' + EBAY_NS + 'CategoryID')
        if cid:
            return cid
    return None


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
            if k and v:
                specifics += ('<NameValueList><Name>%s</Name><Value>%s</Value></NameValueList>'
                              % (xml_escape(str(k)[:65]), xml_escape(str(v)[:65])))
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
            '<ConditionID>%s</ConditionID>'
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
             xml_escape(str(d.get('condition_id') or '3000')),
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
