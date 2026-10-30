"""Four wire protocols in three API families; all requests use a local mock."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exposure_check import encode_png
from vision_review import VisionConfig, PROTOCOLS, build_request, parse_response, request_review, save_profile, load_profile, validate_review

VALUE = dict(classification='overexposed', confidence=.9, reason='面部高光区域细节丢失。', intentional_lighting=False, regions=['面部高光'])


def response(protocol, value=VALUE):
    text = json.dumps(value, ensure_ascii=False)
    return {'openai_chat': {'choices': [{'message': {'content': text}}]},
            'openai_responses': {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]},
            'anthropic': {'content': [{'type': 'text', 'text': text}]},
            'gemini': {'candidates': [{'content': {'parts': [{'text': text}]}}]}}[protocol]


class VisionProtocolTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.received.append((self.path, dict(self.headers), payload))
                if self.path == '/redirect':
                    self.send_response(302); self.send_header('Location', '/v1/chat/completions'); self.end_headers(); return
                if self.path.endswith('/messages'): protocol = 'anthropic'
                elif self.path.endswith(':generateContent'): protocol = 'gemini'
                elif self.path.endswith('/responses'): protocol = 'openai_responses'
                else: protocol = 'openai_chat'
                body = json.dumps(response(protocol), ensure_ascii=False).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.png = encode_png(2, 2, bytes((128, 128, 128)) * 4)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def test_all_native_formats_send_images_and_parse_model_judgments(self):
        for protocol in PROTOCOLS:
            with self.subTest(protocol=protocol):
                base = self.base + ('/v1beta' if protocol == 'gemini' else '/v1')
                result = request_review(VisionConfig(protocol, base, 'fixture-model', json_mode=True), 'unit-key', self.png, {'p50': .7})
                self.assertEqual(result, VALUE)
                path, headers, body = self.received[-1]
                key_header = 'X-api-key' if protocol == 'anthropic' else 'X-goog-api-key' if protocol == 'gemini' else 'Authorization'
                headers = {k.lower(): v for k, v in headers.items()}
                self.assertEqual(headers[key_header.lower()], 'unit-key' if protocol in {'anthropic', 'gemini'} else 'Bearer unit-key')
                self.assertNotIn('unit-key', json.dumps(body))
                if protocol == 'openai_chat': self.assertIn('image_url', json.dumps(body))
                elif protocol == 'openai_responses': self.assertIn('input_image', json.dumps(body))
                elif protocol == 'anthropic': self.assertIn('media_type', json.dumps(body))
                else: self.assertIn('inlineData', json.dumps(body))

    def test_keys_are_not_saved_or_accepted_in_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            config = VisionConfig('gemini', self.base + '/v1beta', 'model')
            save_profile(path, config)
            self.assertEqual(load_profile(path), config)
            self.assertNotIn('key', path.read_text('utf-8').lower())
        for base in (self.base + '?key=secret', 'https://user:secret@example.com/v1', self.base + '/v1#secret'):
            with self.assertRaises(ValueError): VisionConfig(base_url=base, model='test').validate()

    def test_redirects_do_not_forward_credentials(self):
        from urllib.error import HTTPError
        from urllib.request import build_opener
        from vision_review import NoRedirect
        request = build_request(VisionConfig(base_url=self.base + '/v1', model='test'), 'unit-key', self.png, {})
        request.full_url = self.base + '/redirect'
        with self.assertRaises(HTTPError): build_opener(NoRedirect()).open(request)
        self.assertEqual(len(self.received), 1)

    def test_invalid_or_blocked_model_output_is_not_mislabeled_as_normal(self):
        for protocol in PROTOCOLS:
            with self.assertRaises(ValueError): parse_response(protocol, {'error': {'message': 'blocked'}})
        for value in (dict(VALUE, confidence=float('nan')), dict(VALUE, classification='safe'), dict(VALUE, reason=''), dict(VALUE, extra='injection')):
            with self.assertRaises(ValueError): validate_review(value)

    def test_model_response_can_explain_a_dark_stage_as_intentional(self):
        value = dict(VALUE, classification='normal', intentional_lighting=True, reason='舞台背景暗，主体面部仍保留细节。')
        for protocol in PROTOCOLS:
            self.assertEqual(parse_response(protocol, response(protocol, value)), value)


if __name__ == '__main__': unittest.main()
