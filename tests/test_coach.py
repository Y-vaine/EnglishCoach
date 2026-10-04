import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import coach

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        (self.vault/'.obsidian').mkdir()
        self.c = coach.Coach(self.vault)
        self.c.init()
        self.s = self.c.start({'id':'s-test','title':'Synthetic test'})['state']

    def tearDown(self):
        self.tmp.cleanup()

    def turn(self):
        return {'id':'s-test','version':1,'turn':{'id':1,'user':'I work on projects.','coach':'What is your next step?','hints':False}}

    def test_append_idempotency_and_conflict(self):
        a = self.c.append(self.turn())
        self.assertEqual(a['state']['version'],2)
        self.assertTrue(self.c.append(self.turn())['duplicate'])
        conflict = self.turn(); conflict['turn']['user']='Different'
        with self.assertRaises(ValueError): self.c.append(conflict)
        self.assertEqual(len(self.c.find('s-test')[1]['turns']),1)

    def test_preserve_notes_and_reject_manual_changes(self):
        p, _ = self.c.find('s-test')
        with p.open('a',encoding='utf-8') as f: f.write('Personal note')
        self.c.append(self.turn())
        self.assertIn('Personal note',p.read_text(encoding='utf-8'))
        coach.atomic(p,p.read_text(encoding='utf-8').replace('I work on projects.','Edited',1))
        with self.assertRaises(ValueError): self.c.find('s-test')

    def test_stale_version_and_finish_retry(self):
        self.c.append(self.turn())
        payload = dict(id='s-test',version=1,summary='Synthetic summary')
        with self.assertRaises(ValueError): self.c.finish(payload)
        payload['version']=2
        self.c.finish(payload)
        self.assertTrue(self.c.finish(payload)['duplicate'])
        self.assertEqual(self.c.find('s-test')[1]['status'],'completed')

    def test_card_limit_review_and_retry(self):
        a=self.c.card(dict(session_id='s-test',title='Phrase',content='Could you give me an update?'))['state']
        self.assertTrue(self.c.card(dict(session_id='s-test',title='Phrase',content=a['content']))['duplicate'])
        for i in range(2): self.c.card(dict(session_id='s-test',title='Phrase',content=str(i)))
        with self.assertRaises(ValueError): self.c.card(dict(session_id='s-test',title='Fourth',content='4'))
        r=dict(id=a['id'],version=1,event_id='review-1',result='good')
        self.c.review(r)
        self.assertTrue(self.c.review(r)['duplicate'])
        self.assertEqual(len(self.c.find(a['id'])[1]['reviews']),1)
        r['result']='again'
        with self.assertRaises(ValueError): self.c.review(r)

    def test_concurrent_lock_rejected(self):
        with coach.lock(self.c.base):
            with self.assertRaises(ValueError):
                with coach.lock(self.c.base): pass
        with coach.lock(self.c.base): pass

    def test_atomic_failure_preserves_original(self):
        p=self.vault/'note.md'; coach.atomic(p,'original')
        with patch('coach.os.replace',side_effect=OSError('simulated')):
            with self.assertRaises(OSError): coach.atomic(p,'new')
        self.assertEqual(p.read_text(),'original')
        self.assertFalse(list(self.vault.glob('englishcoach-write-*.tmp')))

    def test_missing_vault_and_invalid_id(self):
        with self.assertRaises(ValueError): coach.Coach(self.vault/'missing')
        with self.assertRaises(ValueError): self.c.start({'id':'../../escape'})

    def test_home_rebuild_keeps_notes_and_weekly(self):
        p=self.c.base/'Home.md'
        with p.open('a',encoding='utf-8') as f: f.write('Keep me')
        self.c.home()
        self.assertIn('Keep me',p.read_text(encoding='utf-8'))
        self.c.finish(dict(id='s-test',version=1,summary='Evidence'))
        self.c.weekly()
        self.assertIn('Evidence',(self.c.base/'Weekly/Overview.md').read_text(encoding='utf-8'))

    def test_redact_removes_derived_material(self):
        self.c.append(self.turn())
        self.c.card(dict(session_id='s-test',title='Phrase',content='I work on projects.'))
        self.c.finish(dict(id='s-test',version=2,summary='I work on projects.'))
        self.c.weekly()
        result=self.c.redact(dict(id='s-test',version=3,turn_ids=[1]))
        self.assertEqual(result['state']['turns'][0]['id'],1)
        self.assertEqual(len(result['removed_cards']),1)
        for p in self.c.base.rglob('*.md'):
            self.assertNotIn('I work on projects.',p.read_text(encoding='utf-8'))

if __name__=='__main__': unittest.main()
