#!/usr/bin/env python3
"""Run the actual audit with a fake gh executable; no network or write access."""
import base64
from datetime import datetime, timezone
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
    if os.environ['DRY_RUN'] == 'true':
        sys.exit('unexpected write in dry-run audit')
    with open(os.environ['AUDIT_WRITES'], 'a') as out:
        out.write(json.dumps(args)+'\n')
    print('{"number":123}')
    sys.exit(0)
path = args[1]
route = path.split('?')[0]
faults = json.loads(os.environ['AUDIT_FAULTS'])
meta = dict(name='demo', archived=False, default_branch='master', fork=False,
            description='Fixture', visibility='private', has_issues=True,
            has_wiki=False, allow_merge_commit=True, allow_squash_merge=True,
            allow_rebase_merge=True, delete_branch_on_merge=True,
            allow_update_branch=True)
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
    data = dict(total_count=0, repositories=[])
elif route.endswith('/actions/secrets'):
    data = dict(total_count=1 if '/demo/' in route else 0,
                secrets=[dict(name='KEEP_ME')] if '/demo/' in route else [])
elif route.endswith('/actions/variables'):
    data = dict(total_count=0, variables=[])
elif route.endswith('/environments'):
    data = dict(total_count=0, environments=[])
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
    if isinstance(data, dict) and '__pages__' in data:
        data = data['__pages__'] if '--paginate' in args and '--slurp' in args else data['__pages__'][0]
    elif '--slurp' in args: data = [data]
    if '--jq' in args:
        r = subprocess.run(['jq','-r',args[args.index('--jq')+1]],input=json.dumps(data),text=True)
        sys.exit(r.returncode)
    print(json.dumps(data))
'''


class AuditTests(unittest.TestCase):
    def run_audit(self, faults=None, disable_corpus_guards=False, files=None, dry_run=True, assignees=None):
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
            (root / 'assignees.json').write_text(json.dumps({'assignees': assignees or {}}))
            env = dict(os.environ, PATH=str(root)+os.pathsep+os.environ['PATH'],
                       GH_TOKEN='offline-fixture', ORG='Fixture', DRY_RUN=str(dry_run).lower(),
                       BASELINE_CONFIG='Bitcredit baseline', GITHUB_REPOSITORY='Fixture/.github',
                       LICENSE_JSON=str(root/'license.json'), ASSIGNEES_JSON=str(root/'assignees.json'),
                       GITHUB_STEP_SUMMARY=str(root/'summary'), AUDIT_FAULTS=json.dumps(responses),
                       AUDIT_WRITES=str(root/'writes'))
            script = Path(__file__).with_name('audit-repo-settings.sh')
            if disable_corpus_guards:
                control = root/'audit-repo-settings.sh'
                control.write_text(script.read_text().replace('[ -n "$workflows_complete" ] && ', ''))
                script = control
            result = subprocess.run(['bash',str(script)],
                                    env=env, text=True, capture_output=True, timeout=45)
            self.assertEqual(result.returncode, 0, result.stdout+'\n'+result.stderr)
            self.assertNotIn('unhandled fixture', result.stderr)
            summary = (root/'summary').read_text()
            writes = [json.loads(line) for line in (root/'writes').read_text().splitlines()] if (root/'writes').exists() else []
            return summary if dry_run else (summary, writes)

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

    def test_malformed_tree_rows_leave_dependent_checks_unmeasured(self):
        files = {'.github/workflows/check.yml': 'on: push\njobs: {}\n# secrets.ORG_ONLY\n'}
        valid = dict(path='.github/workflows/check.yml', type='blob', sha='a'*40)
        hidden = dict(path='.github/workflows/hidden.yml', type='blob', sha='b'*40)
        malformed = [None, {k: v for k, v in hidden.items() if k != 'type'},
                     {**hidden, 'type': 'unknown'}, {**hidden, 'path': None}, {**hidden, 'path': ''},
                     {**hidden, 'path': 'invalid\nworkflow.yml'}, {**hidden, 'sha': None},
                     {**hidden, 'sha': 'not-a-sha'}, valid]
        base = {'orgs/Fixture/actions/secrets': [200, {'total_count': 1, 'secrets': [{'name': 'ORG_ONLY'}]}],
                'repos/Fixture/.github/issues': [200, [{'number': 123, 'title': 'Repository settings drift'}]]}
        for row in malformed:
            with self.subTest(row=row):
                summary, writes = self.run_audit({**base, 'repos/Fixture/demo/git/trees/master':
                    [200, {'truncated': False, 'tree': [valid, row]}]}, files=files, dry_run=False)
                self.assertIn('workflow tree', summary)
                self.assertIn('coverage is incomplete', summary)
                self.assertNotIn('— safe to delete', summary)
                self.assertNotIn('references organisation secret(s)', summary)
                self.assertEqual(writes, [])

    def test_malformed_other_branch_rows_cannot_prove_credential_absence(self):
        hidden = dict(path='.github/workflows/hidden.yml', type='blob', sha='b'*40)
        for row in ({k: v for k, v in hidden.items() if k != 'type'},
                    {**hidden, 'path': None}, {**hidden, 'sha': None}, {**hidden, 'sha': '../invalid'}):
            with self.subTest(row=row):
                faults = {'repos/Fixture/demo/branches': [200, [{'name': 'master'}, {'name': 'feature'}]],
                          'repos/Fixture/demo/git/trees/feature': [200, {'truncated': False, 'tree': [row]}]}
                summary = self.run_audit(faults, files={'.github/workflows/check.yml': 'on: push\njobs: {}\n'})
                self.assertIn('UNKNOWN', summary)
                self.assertNotIn('— safe to delete', summary)

    def test_valid_tree_kinds_and_empty_tree_remain_measured(self):
        rows = [dict(path='.github', type='tree', sha='b'*40),
                dict(path='submodule', type='commit', sha='c'*40),
                dict(path='.github/workflows/check.yml', type='blob', sha='a'*40)]
        for tree in (rows, []):
            with self.subTest(tree=tree):
                summary = self.run_audit({'repos/Fixture/demo/git/trees/master': [200, {'truncated': False, 'tree': tree}]},
                                        files={'.github/workflows/check.yml': 'on: push\njobs: {}\n# secrets.KEEP_ME\n'})
                self.assertNotIn('Not measured on this run', summary)

    def test_incomplete_train_is_unknown(self):
        summary = self.run_audit({'repos/Fixture/Clowder/git/matching-refs/tags/v':[500,{}]})
        self.assertIn('Not measured on this run', summary)
        self.assertNotIn('was cut in', summary)

    def test_pages_and_archived_counts_are_not_false_zeroes(self):
        for route, data in (('repos/Fixture/demo/pages',[500,{}]),
                            ('repos/Fixture/demo/pages',[429,{}]),
                            ('repos/Fixture/archived/actions/secrets',[403,{}]),
                            ('repos/Fixture/archived/actions/secrets',[200,{'total_count':None}]),
                            ('repos/Fixture/demo/actions/secrets',[503,{}])):
            with self.subTest(route=route, data=data):
                summary = self.run_audit({route:data})
                self.assertIn('Not measured on this run', summary)
                self.assertNotIn('archived, but still holds', summary)

    def test_pages_are_read_independently_for_every_repository(self):
        meta = dict(archived=False, default_branch='master', fork=False, description='Fixture',
                    visibility='private', has_issues=True, has_wiki=False, allow_merge_commit=True,
                    allow_squash_merge=True, allow_rebase_merge=True, delete_branch_on_merge=True,
                    allow_update_branch=True)
        base = {'orgs/Fixture/repos': [200, [{**meta, 'name': name} for name in ('.github', 'demo')]],
                'repos/Fixture/.github': [200, {**meta, 'name': '.github'}],
                'orgs/Fixture/properties/values': [200, [dict(repository_name=name,
                    properties=[dict(property_name='stack', value='infra')]) for name in ('.github', 'demo')]],
                'repos/Fixture/demo/pages': [200, {'html_url': 'https://example.test/demo/', 'public': True,
                                                'source': {'branch': 'gh-pages'}}]}
        for response in ([403, {}], [429, {}], [503, {}], [200, {'html_url': None, 'public': True}]):
            with self.subTest(response=response):
                summary = self.run_audit({**base, 'repos/Fixture/.github/pages': response})
                self.assertIn('Not measured on this run', summary)
                self.assertIn('private repository publishes a public Pages site at https://example.test/demo/', summary)
        for response in ([404, {}], [200, {'html_url': 'https://example.test/private/', 'public': False}]):
            with self.subTest(response=response):
                summary = self.run_audit({**base, 'repos/Fixture/.github/pages': response})
                self.assertNotIn('Not measured on this run', summary)
                self.assertIn('public Pages site at https://example.test/demo/', summary)
                self.assertNotIn('public Pages site at https://example.test/private/', summary)

    def test_failed_community_reads_cannot_close_the_existing_issue(self):
        files = {'.github/workflows/check.yml': 'on: push\njobs: {}\n# secrets.KEEP_ME\n',
                 'CONTRIBUTING.md': 'Contribution guide.\n'}
        faults = {'repos/Fixture/.github/contents/CONTRIBUTING.md': [200, files['CONTRIBUTING.md']],
                  'repos/Fixture/.github/issues': [200, [{'number': 123, 'title': 'Repository settings drift'}]]}
        self.assertIn('byte-identical', self.run_audit(faults, files=files))
        for route, status in (('repos/Fixture/.github/contents/CONTRIBUTING.md', 503),
                              ('repos/Fixture/demo/contents/CONTRIBUTING.md', 503),
                              ('repos/Fixture/demo/contents/CONTRIBUTING.md', 404)):
            with self.subTest(route=route, status=status):
                summary, writes = self.run_audit({**faults, route: [status, {}]}, files=files, dry_run=False)
                self.assertIn('coverage is incomplete', summary)
                self.assertNotIn('byte-identical', summary)
                self.assertEqual(writes, [])
        # Confirmed absence is still a complete read, so valid issue closure works.
        summary, writes = self.run_audit({**faults, 'repos/Fixture/.github/contents/CONTRIBUTING.md': [404, {}]},
                                        files=files, dry_run=False)
        self.assertIn('No drift found.', summary)
        self.assertTrue(any('state=closed' in call for call in writes))

    def test_failed_configuration_reads_are_not_absence_findings(self):
        files = {'.github/workflows/check.yml': 'on: push\njobs: {}\n# secrets.KEEP_ME\n',
                 'package.json': '{}'}
        for route, false_finding in (('license', 'no LICENSE'),
                                    ('contents/.github/dependabot.yml', 'no .github/dependabot.yml'),
                                    ('code-security-configuration', 'security configuration is')):
            for status in (403, 503):
                with self.subTest(route=route, status=status):
                    summary = self.run_audit({'repos/Fixture/demo/'+route: [status, {}]}, files=files)
                    self.assertIn('Not measured on this run', summary)
                    self.assertNotIn(false_finding, summary)
        summary = self.run_audit({'repos/Fixture/demo/license': [404, {}],
                                 'repos/Fixture/demo/code-security-configuration': [404, {}]}, files=files)
        self.assertIn('no LICENSE', summary)
        self.assertIn('no .github/dependabot.yml', summary)
        self.assertIn("security configuration is 'none'", summary)

    def test_shared_blob_keeps_each_live_branch_reference(self):
        sha = 'b'*40
        tree = {'truncated': False, 'tree': [{'path': '.github/workflows/use.yml', 'type': 'blob', 'sha': sha}]}
        faults = {'repos/Fixture/demo/git/trees/dead': [200, tree],
                  'repos/Fixture/demo/git/trees/live': [200, tree],
                  'repos/Fixture/demo/git/blobs/'+sha: [200, {'content': base64.b64encode(b'# secrets.KEEP_ME\n').decode()}],
                  'repos/Fixture/demo/branches/dead': [200, {'commit': {'commit': {'committer': {'date': '2020-01-01T00:00:00Z'}}}}],
                  'repos/Fixture/demo/branches/live': [200, {'commit': {'commit': {'committer': {'date': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}}}}]}
        for branches in (['master', 'dead', 'live'], ['master', 'live', 'dead']):
            faults['repos/Fixture/demo/branches'] = [200, [{'name': b} for b in branches]]
            summary = self.run_audit(faults, files={'.github/workflows/check.yml': 'on: push\njobs: {}\n'})
            self.assertIn('still referenced on: live', summary)
            self.assertNotIn('— safe to delete', summary)
        faults['repos/Fixture/demo/branches'] = [200, [{'name': 'master'}, {'name': 'dead'}]]
        summary = self.run_audit(faults, files={'.github/workflows/check.yml': 'on: push\njobs: {}\n'})
        self.assertIn('recorded commit is over 90 days old', summary)
        self.assertIn('confirm those branches are unused before deletion', summary)
        self.assertNotIn('— safe to delete', summary)

    def test_truncated_other_branch_cannot_prove_credential_absence(self):
        faults = {'repos/Fixture/demo/branches': [200, [{'name': 'master'}, {'name': 'feature'}]],
                  'repos/Fixture/demo/git/trees/feature': [200, {'truncated': True, 'tree': []}]}
        summary = self.run_audit(faults, files={'.github/workflows/check.yml': 'on: push\njobs: {}\n'})
        self.assertIn('UNKNOWN', summary)
        self.assertNotIn('— safe to delete', summary)

    def test_environment_pages_are_complete_before_reporting_or_closing(self):
        first = [{'name': 'env'+str(i), 'protection_rules': []} for i in range(100)]
        pages = [{'total_count': 101, 'environments': first},
                 {'total_count': 101, 'environments': [{'name': 'last', 'protection_rules': []}]}]
        faults = {'repos/Fixture/demo/environments': [200, {'__pages__': pages}],
                  **{'repos/Fixture/demo/environments/'+e['name']+'/secrets': [200, {'total_count': 0}] for e in first},
                  'repos/Fixture/demo/environments/last/secrets': [200, {'total_count': 1}]}
        summary = self.run_audit(faults)
        self.assertIn("environment 'last' holds 1 secret(s)", summary)
        self.assertIn('plus 1 environment-level', summary)
        faults['repos/Fixture/demo/environments'] = [200, pages[0]]
        faults['repos/Fixture/.github/issues'] = [200, [{'number': 123, 'title': 'Repository settings drift'}]]
        summary, writes = self.run_audit(faults, dry_run=False)
        self.assertIn('coverage is incomplete', summary)
        self.assertIn('Not measured on this run', summary)
        self.assertEqual(writes, [])

    def test_incomplete_credential_lists_do_not_judge_usage_or_grants(self):
        faults = {'orgs/Fixture/actions/secrets': [200, {'total_count': 1, 'secrets': [{'name': 'ORG_ONLY'}]}],
                  'repos/Fixture/demo/actions/secrets': [200, {'total_count': 1, 'secrets': [{'name': 'ORG_ONLY'}]}],
                  'repos/Fixture/demo/contents/.github/workflows/one.yml': [200, 'on: push\njobs: {}\n# secrets.ORG_ONLY\n']}
        for route in ('repos/Fixture/.github/actions/secrets', 'repos/Fixture/demo/actions/secrets',
                      'repos/Fixture/demo/actions/variables'):
            with self.subTest(route=route):
                summary = self.run_audit({**faults, route: [503, {}]})
                self.assertIn('Not measured on this run', summary)
                self.assertNotIn('— safe to delete', summary)
                self.assertNotIn('references organisation secret(s)', summary)

    def test_unreadable_labels_are_not_missing_labels(self):
        config = {'version': 2, 'updates': [{'package-ecosystem': 'github-actions', 'directory': '/',
                  'schedule': {'interval': 'weekly'}, 'assignees': ['owner'], 'labels': ['dependencies']}]}
        faults = {'repos/Fixture/demo/contents/.github/dependabot.yml': [200, {
                  'content': base64.b64encode(json.dumps(config).encode()).decode()}],
                  'repos/Fixture/demo/labels': [200, []]}
        self.assertIn('asks for labels', self.run_audit(faults, assignees={'demo': 'owner'}))
        summary = self.run_audit({**faults, 'repos/Fixture/demo/labels': [503, {}]}, assignees={'demo': 'owner'})
        self.assertIn('Not measured on this run', summary)
        self.assertNotIn('asks for labels', summary)
        for payload in ({}, [{}], [{'name': ''}]):
            summary = self.run_audit({**faults, 'repos/Fixture/demo/labels': [200, payload]}, assignees={'demo': 'owner'})
            self.assertIn('Not measured on this run', summary)
            self.assertNotIn('asks for labels', summary)
        summary = self.run_audit({**faults, 'repos/Fixture/demo/labels': [200, [{'name': 'dependencies'}]]},
                                 assignees={'demo': 'owner'})
        self.assertNotIn('Not measured on this run', summary)
        self.assertNotIn('asks for labels', summary)

    def test_malformed_name_envelopes_do_not_judge_credentials(self):
        faults = {'orgs/Fixture/actions/secrets': [200, {'total_count': 1, 'secrets': [{'name': 'ORG_ONLY'}]}],
                  'repos/Fixture/demo/actions/secrets': [200, {'total_count': 1, 'secrets': [{'name': 'ORG_ONLY'}]}],
                  'repos/Fixture/demo/contents/.github/workflows/one.yml': [200, 'on: push\njobs: {}\n# secrets.ORG_ONLY\n']}
        cases = [('repos/Fixture/demo/actions/secrets', {'total_count': 1}),
                 ('repos/Fixture/demo/actions/secrets', {'total_count': 1, 'secrets': [{}]}),
                 ('repos/Fixture/demo/actions/secrets', {'total_count': 2, 'secrets': [{'name': 'ORG_ONLY'}]}),
                 ('repos/Fixture/demo/actions/variables', {'total_count': 1, 'variables': [{}]}),
                 ('orgs/Fixture/actions/secrets', {'total_count': 1}),
                 ('orgs/Fixture/actions/secrets/ORG_ONLY/repositories', {'total_count': 1})]
        for route, payload in cases:
            with self.subTest(route=route, payload=payload):
                summary = self.run_audit({**faults, route: [200, payload]})
                self.assertIn('Not measured on this run', summary)
                self.assertNotIn('— safe to delete', summary)
                self.assertNotIn('references organisation secret(s)', summary)
        # Both genuine empty lists and valid names remain measured.
        summary = self.run_audit({'repos/Fixture/demo/actions/secrets': [200, {'total_count': 0, 'secrets': []}]})
        self.assertNotIn('Not measured on this run', summary)
        summary = self.run_audit(faults)
        self.assertNotIn('Not measured on this run', summary)
        self.assertNotIn('references organisation secret(s)', summary)

    def test_blob_decoding_keeps_unknown_and_empty_distinct(self):
        workflow = 'on: push\njobs: {}\n'
        for location in ('branch', 'config'):
            files = {'.github/workflows/check.yml': workflow}
            faults = {}
            sha = 'a'*40
            if location == 'branch':
                sha = 'b'*40
                faults = {'repos/Fixture/demo/branches': [200, [{'name': 'master'}, {'name': 'feature'}]],
                          'repos/Fixture/demo/git/trees/feature': [200, {'truncated': False, 'tree': [
                              {'path': '.github/workflows/use.yml', 'type': 'blob', 'sha': sha}]}],
                          'repos/Fixture/demo/branches/feature': [200, {'commit': {'commit': {'committer': {
                              'date': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}}}}]}
            else:
                files['settings.json'] = '{}'
            for content in (None, '%%%'):
                with self.subTest(location=location, content=content):
                    summary = self.run_audit({**faults, 'repos/Fixture/demo/git/blobs/'+sha: [200, {'content': content}]}, files=files)
                    self.assertIn('UNKNOWN', summary)
                    self.assertNotIn('— safe to delete', summary)
            summary = self.run_audit({**faults, 'repos/Fixture/demo/git/blobs/'+sha: [200, {'content': ''}]}, files=files)
            self.assertNotIn('UNKNOWN', summary)
            self.assertIn('— safe to delete', summary)
            content = base64.b64encode(b'# secrets.KEEP_ME\n').decode()
            summary = self.run_audit({**faults, 'repos/Fixture/demo/git/blobs/'+sha: [200, {'content': content}]}, files=files)
            self.assertNotIn('UNKNOWN', summary)
            self.assertNotIn('— safe to delete', summary)

    def test_empty_configuration_name_cannot_close_the_issue(self):
        faults = {'repos/Fixture/demo/code-security-configuration': [200, {'configuration': {'name': ''}}],
                  'repos/Fixture/.github/issues': [200, [{'number': 123, 'title': 'Repository settings drift'}]]}
        summary, writes = self.run_audit(faults, dry_run=False)
        self.assertIn('coverage is incomplete', summary)
        self.assertEqual(writes, [])

    def test_malformed_protection_rules_are_not_fabricated_gates(self):
        faults = {'repos/Fixture/demo/environments/release/secrets': [200, {'total_count': 1}],
                  'repos/Fixture/.github/issues': [200, [{'number': 123, 'title': 'Repository settings drift'}]]}
        malformed = [[None], [{'type': 'required_reviewers'}],
                     [{'type': 'required_reviewers', 'reviewers': []}],
                     [{'type': 'required_reviewers', 'reviewers': [None]}],
                     [{'type': 'required_reviewers', 'reviewers': [{'type': 'User', 'reviewer': {}}]}],
                     [{'type': 'wait_timer', 'wait_timer': '5'}], [{'type': 'unknown_future_rule'}]]
        for rules in malformed:
            with self.subTest(rules=rules):
                response = {'total_count': 1, 'environments': [{'name': 'release', 'protection_rules': rules}]}
                summary, writes = self.run_audit({**faults, 'repos/Fixture/demo/environments': [200, response]}, dry_run=False)
                self.assertIn('coverage is incomplete', summary)
                self.assertNotIn('behind an approval gate:', summary)
                self.assertEqual(writes, [])
        for rules in ([], [{'type': 'branch_policy'}], [{'type': 'wait_timer', 'wait_timer': 5}],
                      [{'type': 'required_reviewers', 'reviewers': [{'type': 'User', 'reviewer': {'login': 'owner'}}]}]):
            response = {'total_count': 1, 'environments': [{'name': 'release', 'protection_rules': rules}]}
            summary = self.run_audit({**faults, 'repos/Fixture/demo/environments': [200, response]})
            self.assertNotIn('Not measured on this run', summary)
            self.assertIn('behind an approval gate: **'+('1' if rules else '0')+'**', summary)

    def test_topics_are_validated_before_any_additive_write(self):
        faults = {'repos/Fixture/.github/issues': [200, [{'number': 123, 'title': 'Repository settings drift'}]]}
        for payload in ({}, {'names': None}, {'names': {}}, {'names': ['custom', None]},
                        {'names': ['']}, {'names': ['custom\nother']}):
            with self.subTest(payload=payload):
                summary, writes = self.run_audit({**faults, 'repos/Fixture/demo/topics': [200, payload]}, dry_run=False)
                self.assertIn('coverage is incomplete', summary)
                self.assertEqual(writes, [])
        for names in ([], ['custom']):
            summary, writes = self.run_audit({**faults, 'repos/Fixture/demo/topics': [200, {'names': names}]}, dry_run=False)
            self.assertNotIn('Not measured on this run', summary)
            self.assertTrue(any('PUT' in call and 'repos/Fixture/demo/topics' in call for call in writes))
            self.assertIn('topics corrected: **1**', summary)

    def test_merge_settings_require_booleans_before_enabling(self):
        flags = ['allow_merge_commit', 'allow_squash_merge', 'allow_rebase_merge',
                 'delete_branch_on_merge', 'allow_update_branch']
        meta = dict(name='demo', default_branch='master', fork=False, description='Fixture',
                    visibility='private', has_issues=True, has_wiki=False, **dict.fromkeys(flags, True))
        faults = {'repos/Fixture/.github/issues': [200, [{'number': 123, 'title': 'Repository settings drift'}]]}
        malformed = [{key: value for key, value in meta.items() if key != missing} for missing in flags]
        malformed += [{**meta, 'allow_merge_commit': value} for value in (None, 'false', 0)]
        for payload in malformed:
            with self.subTest(payload=payload):
                summary, writes = self.run_audit({**faults, 'repos/Fixture/demo': [200, payload]}, dry_run=False)
                self.assertIn('coverage is incomplete', summary)
                self.assertEqual(writes, [])
        summary, writes = self.run_audit({**faults, 'repos/Fixture/demo': [200, {**meta, **dict.fromkeys(flags, False)}]}, dry_run=False)
        self.assertNotIn('Not measured on this run', summary)
        self.assertTrue(any('PATCH' in call and 'repos/Fixture/demo' in call and
                            all(flag+'=true' in call for flag in flags) for call in writes))
        self.assertIn('merge settings corrected: **1**', summary)


if __name__ == '__main__':
    unittest.main()
