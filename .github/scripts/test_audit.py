#!/usr/bin/env python3
"""Run the actual audit with a fake gh executable; no network or write access."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


FAKE_GH = r'''#!/usr/bin/env python3
import base64, json, os, subprocess, sys
args = sys.argv[1:]
if '-X' in args or '--method' in args:
    sys.exit('unexpected write in dry-run audit')
path = args[1]
route = path.split('?')[0]
faults = json.loads(os.environ['AUDIT_FAULTS'])
meta = dict(name='demo', archived=False, default_branch='master', fork=False,
            description='Fixture', visibility='private', has_issues=True,
            has_wiki=False, allow_merge_commit=True, allow_squash_merge=True,
            allow_rebase_merge=True, delete_branch_on_merge=True)
raw = any('application/vnd.github.raw' in arg for arg in args)
status = 200
if route in faults:
    status, data = faults[route]
elif route == 'orgs/Fixture/repos':
    data = [meta, dict(name='archived', archived=True)]
elif route.endswith('/properties/values'):
    data = [dict(repository_name='demo', properties=[dict(property_name='stack',value='infra')])]
elif route.endswith('/members'):
    data = [dict(login='owner')]
elif route.endswith('/actions/secrets/ORG_ONLY/repositories'):
    data = dict(repositories=[])
elif route.endswith('/actions/secrets'):
    data = dict(total_count=1 if '/demo/' in route else 0,
                secrets=[dict(name='KEEP_ME')] if '/demo/' in route else [])
elif route.endswith('/actions/variables'):
    data = dict(total_count=0, variables=[])
elif route.endswith('/environments'):
    data = dict(environments=[])
elif route.endswith('/pages'):
    status, data = 404, dict(message='Not Found', status='404')
elif route == 'repos/Fixture/demo':
    data = meta
elif route.endswith('/topics'):
    data = dict(names=['bitcoin', 'bitcredit'])
elif route.endswith('/license'):
    data = dict(content=base64.b64encode(b'Copyright (c) Fixture\n').decode())
elif '/git/trees/' in route:
    data = dict(truncated=False, tree=[dict(type='blob',path='.github/workflows/'+n,sha='a'*40)
                                     for n in ('one.yml','two.yml')])
elif '/contents/.github/workflows/' in route:
    data = 'name: fixture\non: push\npermissions: {}\njobs: {}\n'
    if route.endswith('/two.yml'): data += '# secrets.KEEP_ME\n'
elif route.endswith('/contents/.github/dependabot.yml'):
    status, data = 404, dict(message='Not Found', status='404')
elif '/contents/' in route:
    raw, data = True, ''
elif route.endswith('/code-security-configuration'):
    data = dict(configuration=dict(name='Bitcredit baseline'))
elif route.endswith('/tags'):
    data = [dict(name='v1.2.3')]
elif '/releases/tags/' in route:
    data = dict(id=1)
elif route.endswith('/releases'):
    data = [dict(id=1)]
elif '/git/matching-refs/' in route:
    data = [dict(ref='refs/tags/v1.2.3-2026-09-08')]
elif route.endswith('/branches'):
    data = [dict(name='master')]
elif route.endswith('/pulls') or route.endswith('/dependabot/alerts'):
    data = []
else:
    sys.exit('unhandled fixture endpoint: ' + route)
if status != 200:
    print(json.dumps(data))
    print('gh: mock error (HTTP %d)' % status, file=sys.stderr)
    sys.exit(1)
if raw:
    print(data, end='')
else:
    if '--slurp' in args: data = [data]
    if '--jq' in args:
        r = subprocess.run(['jq','-r',args[args.index('--jq')+1]],input=json.dumps(data),text=True)
        sys.exit(r.returncode)
    print(json.dumps(data))
'''


class AuditTests(unittest.TestCase):
    def run_audit(self, faults=None, disable_corpus_guards=False, files=None):
        responses = {}
        if files is not None:
            responses['repos/Fixture/demo/git/trees/master'] = [200, {
                'truncated': False, 'tree': [dict(type='blob', path=path, sha='a'*40) for path in files]}]
            responses.update({'repos/Fixture/demo/contents/'+path: [200, body] for path, body in files.items()})
        responses.update(faults or {})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh = root / 'gh'
            gh.write_text(FAKE_GH)
            gh.chmod(0o755)
            (root / 'license.json').write_text('{"holder":"Fixture"}')
            (root / 'assignees.json').write_text('{"assignees":{}}')
            env = dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                       GH_TOKEN='offline-fixture', ORG='Fixture', DRY_RUN='true',
                       BASELINE_CONFIG='Bitcredit baseline', GITHUB_REPOSITORY='Fixture/.github',
                       LICENSE_JSON=str(root/'license.json'), ASSIGNEES_JSON=str(root/'assignees.json'),
                       GITHUB_STEP_SUMMARY=str(root/'summary'), AUDIT_FAULTS=json.dumps(responses))
            script = Path(__file__).with_name('audit-repo-settings.sh')
            if disable_corpus_guards:
                control = root/'audit-repo-settings.sh'
                control.write_text(script.read_text().replace('[ -n "$workflows_complete" ] && ', ''))
                script = control
            result = subprocess.run(['bash',str(script)],
                                    env=env, text=True, capture_output=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout+'\n'+result.stderr)
            self.assertNotIn('unhandled fixture', result.stderr)
            return (root/'summary').read_text()

    def test_success_and_confirmed_absence(self):
        summary = self.run_audit()
        self.assertNotIn('Not measured on this run', summary)
        self.assertNotIn('has no GitHub release', summary)
        summary = self.run_audit({'repos/Fixture/demo/releases/tags/v1.2.3':[404,{}]})
        self.assertIn('highest-versioned tag `v1.2.3` has no GitHub release', summary)

    def test_shell_only_workflows_do_not_require_dependabot(self):
        workflow = '''name: Shell checks
on: push
permissions: {}
jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 1
    steps:
      - run: echo hello
'''
        summary = self.run_audit({'repos/Fixture/demo/contents/.github/workflows/one.yml': [200, workflow]})
        self.assertNotIn('no .github/dependabot.yml', summary)
        self.assertNotIn('Not measured on this run', summary)

    def test_external_steps_and_reusable_workflows_require_dependabot(self):
        jobs = [
            {'runs-on': 'ubuntu-latest', 'steps': [{'uses': 'example/action@v2'}]},
            {'runs-on': 'ubuntu-latest', 'steps': [{'uses': 'example/actions/setup@'+'a'*40}]},
            {'uses': 'example/automation/.github/workflows/build.yml@v2'},
            {'uses': 'example/automation/.github/workflows/build.yml@'+'b'*40},
        ]
        for job in jobs:
            with self.subTest(job=job):
                body = '# secrets.KEEP_ME\n' + json.dumps({'on': 'push', 'permissions': {}, 'jobs': {'check': job}})
                summary = self.run_audit(files={'.github/workflows/check.yml': body})
                self.assertIn('no .github/dependabot.yml, but has github-actions', summary)
                self.assertNotIn('Not measured on this run', summary)

    def test_local_refs_comments_run_strings_and_docker_are_not_external_actions(self):
        body = '''# secrets.KEEP_ME
# uses: example/comment-only@v1
name: Local checks
on: push
permissions: {}
jobs:
  local_reusable:
    uses: ./.github/workflows/local.yml
  shell:
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/actions/local
      - uses: docker://alpine:3.22
      - run: |
          echo 'uses: example/string-only@v1'
          uses: example/block-scalar@v1
'''
        files = {'.github/workflows/check.yml': body,
                 '.github/workflows/local.yml': 'on: workflow_call\njobs: {}\n',
                 '.github/actions/local/action.yml': 'runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: echo local\n'}
        summary = self.run_audit(files=files)
        self.assertNotIn('no .github/dependabot.yml', summary)
        self.assertNotIn('Not measured on this run', summary)

    def test_composite_action_yml_and_yaml_external_steps_require_dependabot(self):
        cases = [('.github/actions/helper/'+filename, 'example/action/path@'+'c'*40)
                 for filename in ('action.yml', 'action.yaml')]
        cases += [('.github/actions/'+directory+'/action.yml', 'example/action@v2')
                  for directory in ('helper', 'build')]
        for path, reference in cases:
            with self.subTest(path=path, reference=reference):
                body = 'name: Composite\nruns:\n  using: composite\n  steps:\n    - uses: '+reference+'\n'
                summary = self.run_audit(files={path: body})
                self.assertIn('no .github/dependabot.yml, but has github-actions', summary)
                self.assertNotIn('Not measured on this run', summary)

    def test_malformed_action_dependency_yaml_is_unmeasured(self):
        for path, body in (('.github/workflows/check.yml', 'jobs: [unterminated'),
                           ('.github/actions/helper/action.yml', 'runs: [unterminated')):
            with self.subTest(path=path):
                summary = self.run_audit(files={path: body})
                self.assertIn('Not measured on this run', summary)
                self.assertIn(path, summary)
                self.assertNotIn('no .github/dependabot.yml', summary)

    def test_unreadable_action_sources_are_unmeasured(self):
        for path in ('.github/workflows/check.yml', '.github/actions/helper/action.yaml'):
            for code in (403, 404, 500):
                with self.subTest(path=path, code=code):
                    summary = self.run_audit(
                        {'repos/Fixture/demo/contents/'+path: [code, {'message': 'unavailable'}]},
                        files={path: 'name: Fixture\n'})
                    self.assertIn('Not measured on this run', summary)
                    self.assertIn(path, summary)
                    self.assertNotIn('no .github/dependabot.yml', summary)

    def test_incomplete_action_trees_are_unmeasured(self):
        failures = [[200, {'truncated': True, 'tree': []}]] + [
            [code, {'message': 'unavailable'}] for code in (403, 404, 503)]
        for response in failures:
            with self.subTest(response=response):
                summary = self.run_audit({'repos/Fixture/demo/git/trees/master': response})
                self.assertIn('Not measured on this run', summary)
                self.assertNotIn('no .github/dependabot.yml', summary)

    def test_transport_failures_are_gaps_not_tag_names(self):
        for code in (403, 429, 500):
            with self.subTest(code=code):
                summary = self.run_audit({'repos/Fixture/demo/tags':[code,{'message':'broken','status':str(code)}]})
                self.assertIn('Not measured on this run', summary)
                self.assertNotIn('highest-versioned tag `', summary)

    def test_release_errors_are_not_missing_releases(self):
        for route in ('releases/tags/v1.2.3', 'releases'):
            faults = {'repos/Fixture/demo/releases/tags/v1.2.3':[404,{}],
                      'repos/Fixture/demo/'+route:[503,{'message':'offline'}]}
            summary = self.run_audit(faults)
            self.assertIn('Not measured on this run', summary)
            self.assertNotIn('has no GitHub release', summary)

    def test_invalid_success_payload_and_tag(self):
        for data in ({'message':'invalid'}, [{'name':'invalid tag'}]):
            summary = self.run_audit({'repos/Fixture/demo/tags':[200,data]})
            self.assertIn('invalid', summary)
            self.assertNotIn('highest-versioned tag `', summary)

    def test_incomplete_workflows_do_not_judge_credentials(self):
        faults = {'repos/Fixture/demo/contents/.github/workflows/two.yml':[503,{}],
                  'orgs/Fixture/actions/secrets':[200,{'total_count':1,'secrets':[{'name':'ORG_ONLY'}]}],
                  'repos/Fixture/demo/contents/.github/workflows/one.yml':[200,'# secrets.ORG_ONLY\n']}
        summary = self.run_audit(faults)
        self.assertIn('Not measured on this run', summary)
        self.assertNotIn('safe to delete', summary)
        self.assertNotIn('references organisation secret(s)', summary)
        control = self.run_audit(faults, disable_corpus_guards=True)
        self.assertIn('KEEP_ME is read by no workflow on any branch — safe to delete', control)
        self.assertIn('references organisation secret(s) it was not granted: ORG_ONLY', control)

    def test_incomplete_train_is_unknown(self):
        summary = self.run_audit({'repos/Fixture/Clowder/git/matching-refs/tags/v':[500,{}]})
        self.assertIn('Not measured on this run', summary)
        self.assertNotIn('was cut in', summary)

    def test_pages_and_archived_counts_are_not_false_zeroes(self):
        for route, data in (('repos/Fixture/.github/pages',[500,{}]),
                            ('repos/Fixture/demo/pages',[429,{}]),
                            ('repos/Fixture/archived/actions/secrets',[403,{}]),
                            ('repos/Fixture/archived/actions/secrets',[200,{'total_count':None}]),
                            ('repos/Fixture/demo/actions/secrets',[503,{}])):
            with self.subTest(route=route, data=data):
                summary = self.run_audit({route:data})
                self.assertIn('Not measured on this run', summary)
                self.assertNotIn('archived, but still holds', summary)


if __name__ == '__main__':
    unittest.main()
