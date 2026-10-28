"""Verify stable identity and actual HTTP request bodies without external APIs."""

from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from openai import OpenAI

from agent import Agent
from agent.identity import resolve_user_id, validate_user_id
from agent.memory import MemoryStore
from agent.test.quiet_comm import QuietComm, local_model_network


@contextmanager
def model_capture():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            if body.get('stream'):
                base = {'id': 'offline', 'object': 'chat.completion.chunk', 'created': 0,
                        'model': body['model']}
                chunks = [dict(base, choices=[{'index': 0, 'delta': {'content': 'Ready'}, 'finish_reason': None}]),
                          dict(base, choices=[{'index': 0, 'delta': {}, 'finish_reason': 'stop'}])]
                content = (''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks) + 'data: [DONE]\n\n').encode()
                content_type = 'text/event-stream'
            else:
                prompt = body['messages'][0]['content']
                if prompt.startswith('Select memory'):
                    answer = '[0]'
                elif prompt.startswith('Treat these memory'):
                    answer = json.dumps([{'name': 'Combined', 'type': 'project', 'description': 'Stable consolidated fact', 'body': 'Keep this fact.'}])
                else:
                    answer = '[]'
                content = json.dumps({'id': 'offline', 'object': 'chat.completion', 'created': 0,
                                      'model': body['model'], 'choices': [{'index': 0, 'finish_reason': 'stop',
                                      'message': {'role': 'assistant', 'content': answer}}]}).encode()
                content_type = 'application/json'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class UserIdentityTests(unittest.TestCase):
    def setUp(self):
        network = local_model_network()
        network.start()
        self.addCleanup(network.stop)
        communication = patch("agent.agent.Comm", QuietComm)
        communication.start()
        self.addCleanup(communication.stop)

    def test_profile_identity_is_stable_across_workspace_and_display_name_changes(self):
        with TemporaryDirectory() as directory, model_capture() as (url, _):
            ready = []
            with Agent(api_key='offline', base_url=url, profile_id='profile_1', name='alice',
                       workdir=Path(directory)/'first', host='127.0.0.1', event_sink=ready.append) as first:
                self.assertEqual(first.user_id, 'agent_profile_1')
                self.assertEqual(first.memory.user_id, first.user_id)
                self.assertEqual(ready[0]['user_id'], first.user_id)
            with Agent(api_key='offline', base_url=url, profile_id='profile_1', name='renamed',
                       workdir=Path(directory)/'elsewhere', host='127.0.0.1', enable_memory=False) as second:
                self.assertEqual(second.user_id, 'agent_profile_1')

    def test_standalone_identity_survives_reconstruction_and_is_unique_per_workspace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = resolve_user_id(root/'one')
            self.assertRegex(first, r'^agent_[0-9a-f]{32}$')
            self.assertEqual(resolve_user_id(root/'one'), first)
            self.assertNotEqual(resolve_user_id(root/'two'), first)
            self.assertNotIn(str(root), first)
            self.assertEqual((root/'one'/'.agent-user-id').read_text().strip(), first)

    def test_explicit_and_persisted_invalid_identities_fail_before_model_requests(self):
        with TemporaryDirectory() as directory:
            for value in ('', 'has space', '个人身份', 'id@example.com', 'x'*513, None, 3, True):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'user_id'):
                    validate_user_id(value)
            root = Path(directory)
            (root/'.agent-user-id').write_text('bad id\n')
            with self.assertRaisesRegex(ValueError, 'user_id'):
                resolve_user_id(root)
            with self.assertRaisesRegex(ValueError, 'user_id'):
                Agent(api_key='offline', user_id='bad id', workdir=root)
            self.assertEqual(validate_user_id('a-Z_9'), 'a-Z_9')
            self.assertEqual(len(validate_user_id('x'*512)), 512)

    def test_stream_and_every_memory_operation_put_same_user_id_in_http_body(self):
        with TemporaryDirectory() as directory, model_capture() as (url, requests):
            for profile_id, thinking in (('alice_1', True), ('bobe_2', False)):
                with Agent(api_key='offline', base_url=url, api_mode='deepseek', profile_id=profile_id, thinking=thinking,
                           workdir=Path(directory)/profile_id, host='127.0.0.1') as node:
                    before = len(requests)
                    with redirect_stdout(StringIO()):
                        self.assertEqual(node._generate_response([{'role': 'user', 'content': 'hello'}])['content'], 'Ready')
                    memory = node.memory
                    for index in range(10):
                        memory.write_memory_file(f'Fact {index}', 'project', f'Stable fact {index}', f'Fact content {index}')
                    self.assertTrue(memory.load_memories([{'role': 'user', 'content': 'Recall facts'}]))
                    self.assertEqual(memory.extract_memories([{'role': 'user', 'content': 'A new stable fact'}]), 0)
                    self.assertEqual(memory.consolidate_memories(), 1)
                    batch = requests[before:]
                    self.assertEqual(len(batch), 4)
                    self.assertEqual([request['stream'] for request in batch], [True, False, False, False])
                    self.assertTrue(all(request['user_id'] == 'agent_'+profile_id for request in batch))
                    self.assertEqual(batch[0]['thinking']['type'], 'enabled' if thinking else 'disabled')
                    self.assertTrue(all(request['thinking']['type'] == 'disabled' for request in batch[1:]))
                    self.assertTrue(all('extra_body' not in request and 'user' not in request for request in batch))
            self.assertEqual({request['user_id'] for request in requests}, {'agent_alice_1', 'agent_bobe_2'})

    def test_memory_store_alone_uses_same_persistent_workspace_identity(self):
        with TemporaryDirectory() as directory, model_capture() as (url, requests):
            with OpenAI(api_key='offline', base_url=url) as client:
                first = MemoryStore(Path(directory), client, 'deepseek-flash', api_mode='deepseek')
                second = MemoryStore(Path(directory), client, 'deepseek-flash', api_mode='deepseek')
                first.extract_memories([{'role': 'user', 'content': 'A stable preference'}])
                self.assertEqual(first.user_id, second.user_id)
                self.assertEqual(requests[0]['user_id'], first.user_id)


if __name__ == '__main__':
    unittest.main()
