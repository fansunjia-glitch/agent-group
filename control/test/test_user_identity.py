"""Control profile identity persistence and real worker request propagation."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control.service import ControlError, ControlService
from control.test.test_control import ControlCase, fake_model, profile_data


class ControlIdentityTests(ControlCase):
    def test_old_profiles_migrate_once_and_identity_survives_rename_and_restart(self):
        service = self.service()
        first = service.save_profile(profile_data('alice'))
        second = service.save_profile(profile_data('bobe'))
        self.assertEqual(first['user_id'], 'agent_'+first['id'])
        self.assertNotEqual(first['user_id'], second['user_id'])
        renamed = service.save_profile(profile_data('renamed'), first['id'])
        self.assertEqual(renamed['user_id'], first['user_id'])
        with self.assertRaisesRegex(ControlError, 'cannot be changed'):
            service.save_profile(profile_data('renamed', user_id='different'), first['id'])
        service.close()
        path = Path(self.directory.name)/'control.json'
        saved = json.loads(path.read_text())
        for profile in saved['profiles']:
            profile.pop('user_id')
        path.write_text(json.dumps(saved))
        restored = self.service()
        identities = {p['id']: p['user_id'] for p in restored.snapshot()['profiles']}
        self.assertEqual(identities[first['id']], first['user_id'])
        self.assertEqual(identities[second['id']], second['user_id'])
        self.assertEqual(json.loads(path.read_text())['profiles'][0]['user_id'], first['user_id'])

    def test_persisted_invalid_or_duplicate_identity_is_rejected(self):
        service = self.service()
        one = service.save_profile(profile_data('alice'))
        service.save_profile(profile_data('bobe'))
        service.close()
        path = Path(self.directory.name)/'control.json'
        saved = json.loads(path.read_text())
        saved['profiles'][1]['user_id'] = one['user_id']
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ControlError, 'distinct user_id'):
            self.service()
        saved['profiles'][1]['user_id'] = 'bad id'
        path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, 'user_id'):
            self.service()

    def test_real_worker_sends_profile_user_id_and_ready_reports_it(self):
        with fake_model() as (url, requests):
            service = self.service(background=True)
            profile = service.save_profile(profile_data('alice', base_url=url))
            service.set_credentials({'api_key': 'offline-test'})
            for attempt in range(2):
                before = len(requests)
                service.start_profile(profile['id'])
                self.wait_for(lambda: len(requests) > before, service=service, label='model request')
                self.wait_for(lambda: service.runtime[profile['id']].get('user_id'), service=service, label='ready identity')
                self.assertEqual(requests[-1]['body']['user'], profile['user_id'])
                self.assertNotIn('user_id', requests[-1]['body'])
                self.assertNotIn('thinking', requests[-1]['body'])
                self.assertEqual(service.runtime[profile['id']]['user_id'], profile['user_id'])
                self.assertTrue(any(e['type']=='ready' and e.get('user_id')==profile['user_id'] for e in service.events))
                service.stop_profile(profile['id'])
                self.wait_for(lambda: service.processes[profile['id']]['process'].poll() is not None,
                              service=service, label='worker stopped')
                service.processes[profile['id']]['reader'].join(3)


if __name__ == '__main__':
    unittest.main()
