"""Offline enforcement tests. No credentials or network model calls."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import delegate as d


class Enforcement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pb-delegation-test-')
        self.root = Path(self.tmp.name)
        self.source = self.root/'source'
        self.source.mkdir()
        (self.source/'code.py').write_text('value = 1\n')
        (self.source/'check.py').write_text('assert True\n')
        self.packet = {'mode':'edit','objective':'Synthetic local test','context':['code.py','check.py'],
                       'writable':['code.py','new.py'],'tests':{'check':['/usr/bin/python3','-B','check.py']}}

    def tearDown(self):
        self.tmp.cleanup()

    def session(self):
        return d.Session(self.packet,self.source,self.root/'run')

    def test_paths_secrets_and_symlinks_refused(self):
        for path in ('../escape','/etc/passwd','.env','nested/.git/config','secrets/key'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                d.relative(path)
        (self.source/'link').symlink_to(self.source/'code.py')
        with self.assertRaises(ValueError): d.source_file(self.source,'link')
        with self.assertRaises(ValueError): d.safe_text('sk-'+'x'*24)

    def test_exact_write_list_and_evidence(self):
        session=self.session()
        with self.assertRaises(ValueError): session.tool('edit_file',{'path':'check.py','old_string':'True','new_string':'False'})
        with self.assertRaises(ValueError): session.tool('run_command',{'command':'anything'})
        with self.assertRaises(ValueError): session.tool('read_file',{'path':'elsewhere'})
        with self.assertRaises(ValueError): session.tool('run_test',{'name':'unapproved'})
        session.tool('edit_file',{'path':'code.py','old_string':'1','new_string':'2'})
        evidence=session.read('code.py',1,1)
        self.assertEqual(evidence['excerpt'],'value = 2\n')
        self.assertEqual(evidence['file_sha256'],d.sha(b'value = 2\n'))
        receipt=session.finalize({})
        self.assertEqual(receipt['changed_files'],['code.py'])
        self.assertTrue(receipt['scope_ok'] and receipt['source_unchanged'])
        self.assertEqual((self.source/'code.py').read_text(),'value = 1\n')
        self.assertIn('+value = 2',(session.output/'changes.patch').read_text())

    def test_read_only_and_concurrent_source_drift(self):
        self.packet.update(mode='audit',writable=[])
        session=self.session()
        with self.assertRaises(ValueError): session.tool('write_file',{'path':'code.py','content':'bad'})
        (self.source/'code.py').write_text('concurrent edit\n')
        self.assertFalse(session.finalize({})['source_unchanged'])

    def test_sandbox_no_home_network_or_workspace_write(self):
        session=self.session()
        code = '''import pathlib,socket,os
assert not pathlib.Path('/home/ghostmonday/.grok/secrets.env').exists()
assert 'DEEPSEEK_API_KEY' not in os.environ
assert not pathlib.Path('/run/user').exists()
assert socket.socket().connect_ex(('1.1.1.1',443)) != 0
try: pathlib.Path('/workspace/code.py').write_text('oops')
except OSError: pass
else: raise AssertionError('workspace writable')
print('ISOLATION_PASS')
'''
        result=d.sandbox(session.workspace,['/usr/bin/python3','-I','-B','-c',code])
        self.assertEqual(result['exit_code'],0,result)
        self.assertIn('ISOLATION_PASS',result['output'])
        self.assertEqual(session.tool('run_test',{'name':'check'})['exit_code'],0)

    def test_no_api_for_dry_run_and_budget_stop(self):
        session=self.session()
        with patch.object(d,'resolve_key',return_value='synthetic-key'), patch.object(d,'api') as api:
            receipt=d.run(session,False)
            self.assertEqual(receipt['model_calls'],0)
            receipt=d.run(session,True,budget=0)
            self.assertEqual(receipt['status'],'blocked')
            api.assert_not_called()

    def test_provider_mismatch_stops_before_tools(self):
        session=self.session()
        with patch.object(d,'resolve_key',return_value='synthetic-key'), patch.object(d,'api',return_value={'model':'other-model','usage':{}}):
            receipt=d.run(session,True)
        self.assertEqual(receipt['status'],'blocked')
        self.assertFalse(session.events)

    def test_response_failure_is_not_retried(self):
        session=self.session()
        with patch.object(d,'resolve_key',return_value='synthetic-key'), patch.object(d,'api',side_effect=RuntimeError('DeepSeek HTTP 401')) as api:
            receipt=d.run(session,True)
        self.assertEqual(api.call_count,1)
        self.assertTrue(receipt['possible_unreported_usage'])

    def test_normal_final_after_tools_preserves_report(self):
        session=self.session()
        session.events.append({'tool':'read_file','result':session.read('code.py')})
        response={'model':'deepseek-flash','usage':{},'choices':[{'message':{'role':'assistant','content':'Observed code.py line 1: value = 1'}}]}
        with patch.object(d,'resolve_key',return_value='synthetic-key'), patch.object(d,'api',return_value=response):
            receipt=d.run(session,True)
        self.assertEqual(receipt['finish_protocol'],'assistant_text_after_tools')
        self.assertEqual(receipt['supervisor_acceptance'],'pending')


if __name__=='__main__': unittest.main(verbosity=2)
