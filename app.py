from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app)

@app.route('/')
def index():
    return app.send_static_file('index.html')

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
        return jsonify({'status': resp.status_code, 'body': resp.text, 'json': resp.json() if resp.text else {}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/anthropic', methods=['POST'])
def anthropic_proxy():
    data = request.json
    api_key = data.get('api_key', '')
    payload = data.get('payload', {})
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
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
