"""Provider configuration and secrets stay isolated without real Comm nodes."""

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from http.client import HTTPConnection
import unittest
from unittest.mock import Mock, patch

from control.service import ControlError, ControlService
from control.server import make_server


def provider(name='Vendor', **extra):
    return {'name': name, 'base_url': 'https://model.example/v1', 'model': 'custom-model', **extra}


def profile(provider_id, name='resident'):
    return {'name': name, 'persona': 'Town resident', 'initial_prompt': 'Wait for topics',
            'provider_id': provider_id, 'enable_memory': False}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix='provider-config-test-')
        self.addCleanup(self.directory.cleanup)
        comm = patch('control.service.Comm')
        factory = comm.start()
        self.addCleanup(comm.stop)
        factory.side_effect = lambda **kw: Mock(agent_id='test-admin', endpoint='http://127.0.0.1:1')
        self.services = []
        self.addCleanup(lambda: [service.close() for service in reversed(self.services)])

    def service(self):
        value = ControlService(self.directory.name, background=False)
        self.services.append(value)
        return value

    def test_persisted_metadata_does_not_persist_keys_and_restart_preserves_assignment(self):
        service = self.service()
        config = service.save_model_provider(provider(api_key='provider-secret'))
        person = service.save_profile(profile(config['id']))
        self.assertTrue(config['credentials_configured'])
        self.assertEqual(config['api_mode'], 'openai')
        state = service.snapshot()
        self.assertEqual(state['profiles'][0]['model'], 'custom-model')
        self.assertTrue(state['profiles'][0]['credentials_configured'])
        self.assertNotIn('provider-secret', json.dumps(state))
        disk = service.path.read_text()
        self.assertNotIn('provider-secret', disk)
        self.assertNotIn('api_key', disk)
        service.close()
        restored = self.service()
        self.assertFalse(restored.snapshot()['model_providers'][0]['credentials_configured'])
        self.assertEqual(restored.snapshot()['profiles'][0]['provider_id'], config['id'])
        self.assertEqual(restored.snapshot()['profiles'][0]['user_id'], person['user_id'])
        with self.assertRaisesRegex(ControlError, 'session API key'):
            restored.start_profile(person['id'])

    def test_legacy_profiles_migrate_once_deduplicated_and_keep_identity(self):
        path = Path(self.directory.name) / 'control.json'
        path.write_text(json.dumps({'version': 1, 'profiles': [
            {'id': 'alice', 'name': 'alice', 'host_id': 'local', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash'},
            {'id': 'bobe', 'name': 'bobe', 'host_id': 'local', 'base_url': 'https://api.deepseek.com/', 'model': 'deepseek-flash'},
            {'id': 'cancy', 'name': 'cancy', 'host_id': 'local', 'base_url': 'http://localhost:1234/v1', 'model': 'local-model'},
        ]}))
        service = self.service()
        state = service.snapshot()
        self.assertEqual(len(state['model_providers']), 2)
        self.assertEqual(state['profiles'][0]['provider_id'], state['profiles'][1]['provider_id'])
        self.assertEqual([p['api_mode'] for p in state['profiles']], ['deepseek', 'deepseek', 'openai'])
        ids = [p['id'] for p in state['model_providers']]
        service.close()
        restored = self.service()
        self.assertEqual(ids, [p['id'] for p in restored.snapshot()['model_providers']])
        self.assertEqual(restored.snapshot()['profiles'][0]['user_id'], 'agent_alice')

    def test_key_updates_clear_and_destination_changes_never_reuse_an_old_secret(self):
        service = self.service()
        saved = service.save_model_provider(provider(api_key='old-secret'))
        identifier = saved['id']
        service.save_model_provider(provider(name='Renamed', api_key=''), identifier)
        self.assertEqual(service.provider_keys[identifier], 'old-secret')
        service.save_model_provider(provider(base_url='https://another.example/v1'), identifier)
        self.assertNotIn(identifier, service.provider_keys)
        service.save_model_provider(provider(api_key='new-secret'), identifier)
        service.save_model_provider(provider(clear_api_key=True), identifier)
        self.assertNotIn(identifier, service.provider_keys)
        service._record_event(None, None, {'type': 'error', 'error': 'old-secret / new-secret'})
        self.assertEqual(service.events[-1]['error'], '[redacted] / [redacted]')
        self.assertNotIn('old-secret', (service.root / 'events.jsonl').read_text())
        self.assertNotIn('new-secret', (service.root / 'events.jsonl').read_text())

    def test_failure_to_save_provider_changes_rolls_back_metadata_and_credentials(self):
        service = self.service()
        original = service.save_model_provider(provider(api_key='original'))
        before = service.path.read_text()
        with patch.object(service, '_save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                service.save_model_provider(provider(base_url='https://changed.example/v1', api_key='replacement'), original['id'])
            with self.assertRaises(OSError):
                service.delete_model_provider(original['id'])
            with self.assertRaises(OSError):
                service.save_model_provider(provider(name='Another'))
        self.assertEqual(service.path.read_text(), before)
        self.assertEqual(service.snapshot()['model_providers'], [original])
        self.assertEqual(service.provider_keys, {original['id']: 'original'})
        self.assertNotIn('replacement', service.session_secrets)

    def test_deleting_referenced_provider_is_rejected_and_reassigning_agent_keeps_user_id(self):
        service = self.service()
        first = service.save_model_provider(provider(api_key='key-a'))
        second = service.save_model_provider(provider('Other', model='other-model', api_key='key-b'))
        person = service.save_profile(profile(first['id']))
        with self.assertRaises(ControlError) as error:
            service.delete_model_provider(first['id'])
        self.assertEqual(error.exception.status, 409)
        changed = service.save_profile(profile(second['id']), person['id'])
        self.assertEqual(changed['user_id'], person['user_id'])
        self.assertEqual(changed['model'], 'other-model')
        self.assertEqual(service.delete_model_provider(first['id']), {'deleted': first['id']})
        self.assertNotIn(first['id'], service.provider_keys)
        self.assertIn('key-a', service.session_secrets)

    def test_missing_provider_key_is_not_replaced_by_other_vendor_credentials(self):
        service = self.service()
        first = service.save_model_provider(provider(api_key='key-a'))
        second = service.save_model_provider(provider('Other', base_url='https://other.example/v1'))
        person = service.save_profile(profile(second['id']))
        with self.assertRaises(ControlError) as error:
            service.start_profile(person['id'])
        self.assertEqual(error.exception.status, 409)
        with self.assertRaisesRegex(ControlError, 'provider_id'):
            service.set_credentials({'api_key': 'ambiguous-key'})
        self.assertNotIn('ambiguous-key', service.session_secrets)
        service.set_credentials({'provider_id': second['id'], 'api_key': 'key-b'})
        self.assertEqual(service.provider_keys, {first['id']: 'key-a', second['id']: 'key-b'})

    def test_two_worker_launches_use_their_own_provider_model_url_and_key(self):
        service = self.service()
        configurations = [service.save_model_provider(provider(api_key='key-a')),
                          service.save_model_provider(provider('Other', base_url='https://other.example/v1',
                                                               model='other-model', api_key='key-b', api_mode='deepseek'))]
        configs = []
        for index, selected in enumerate(configurations):
            person = service.save_profile(profile(selected['id'], name=f'resident-{index}'))
            process = Mock(pid=12345 + index, stdin=io.StringIO())
            process.poll.return_value = None
            with patch('control.service.subprocess.Popen', return_value=process) as spawn, patch.object(service, '_read_worker'):
                service.start_profile(person['id'])
            service.processes[person['id']]['reader'].join(2)
            config = json.loads(process.stdin.getvalue().splitlines()[0])['config']
            configs.append(config)
            self.assertEqual((config['base_url'], config['model'], config['api_mode']),
                             (selected['base_url'], selected['model'], selected['api_mode']))
            self.assertNotIn(config['api_key'], repr(spawn.call_args))
            process.poll.return_value = 0
            service.processes[person['id']]['log'].close()
        self.assertEqual([config['api_key'] for config in configs], ['key-a', 'key-b'])
        self.assertNotEqual(configs[0]['user_id'], configs[1]['user_id'])
        self.assertNotIn('key-b', json.dumps(configs[0]))
        self.assertNotIn('key-a', json.dumps(configs[1]))

    def test_old_client_pending_key_is_scoped_to_first_legacy_provider_only(self):
        service = self.service()
        service.set_credentials({'api_key': 'legacy-key'})
        first = service.save_profile({'name': 'old', 'persona': 'resident', 'base_url': 'https://first.example/v1', 'model': 'one'})
        second = service.save_profile({'name': 'other', 'persona': 'resident', 'base_url': 'https://second.example/v1', 'model': 'two'})
        self.assertTrue(first['credentials_configured'])
        self.assertFalse(second['credentials_configured'])
        self.assertEqual(service.provider_keys, {first['provider_id']: 'legacy-key'})

    def test_invalid_provider_configuration_is_rejected_without_mutating_state(self):
        service = self.service()
        for changes in [{'base_url': 'https://key:secret@example.com/v1'}, {'base_url': 'ftp://example.com'}, {'base_url': 'http://example.com:bad'}, {'base_url': 'https://['},
                        {'base_url': 'https://example.com/v1?api_key=secret'}, {'base_url': 'https://example.com/#secret'},
                        {'model': ''}, {'name': ''}, {'api_mode': 'other'}, {'clear_api_key': 'yes'},
                        {'api_key': None}, {'api_key': 'replace', 'clear_api_key': True}]:
            with self.subTest(changes=changes), self.assertRaises(ControlError):
                service.save_model_provider(provider(**changes))
        self.assertEqual(service.snapshot()['model_providers'], [])

    def test_provider_routes_require_token_and_do_not_echo_keys(self):
        service = self.service()
        server = make_server(service, port=0)
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        thread.start()
        def request(method, path, value, token=True):
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            headers = {'Content-Type': 'application/json'}
            if token: headers['X-Control-Token'] = service.csrf_token
            connection.request(method, path, json.dumps(value), headers)
            response = connection.getresponse()
            result = json.loads(response.read())
            connection.close()
            return response.status, result
        try:
            self.assertEqual(request('POST', '/api/model-providers', provider(api_key='private'), False)[0], 403)
            status, created = request('POST', '/api/model-providers', provider(api_key='private'))
            self.assertEqual(status, 200)
            self.assertNotIn('private', json.dumps(created))
            path = '/api/model-providers/' + created['id']
            self.assertEqual(request('PUT', path, provider(model='updated'))[1]['model'], 'updated')
            self.assertEqual(request('DELETE', path, {})[0], 200)
            self.assertEqual(request('DELETE', path, {})[0], 404)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)
