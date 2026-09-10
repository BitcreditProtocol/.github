import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import textwrap

WORKFLOW = Path(__file__).resolve().parents[1] / 'workflows/sync-master-to-dev.yml'
# Read this workflow's one literal shell block, without a YAML dependency.
SCRIPT = textwrap.dedent(WORKFLOW.read_text().split('        run: |\n', 1)[1])

MOCK_GH = '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ['TEST_ROOT'])
with (root / 'gh-calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\\n')
if args[0] == 'api':
    assert '--paginate' in args
    assert args[args.index('--method') + 1] == 'GET'
    assert f"repos/{os.environ['GH_REPO']}/pulls" in args
    assert all(value in args for value in ['state=open', 'base=dev', 'per_page=100'])
    if os.environ.get('TEST_API_FAIL'):
        sys.exit(1)
    if os.environ.get('TEST_ADVANCE_MASTER'):
        subprocess.run(['git', '--git-dir', str(root / 'remote.git'), 'update-ref',
                        'refs/heads/master', os.environ['TEST_ADVANCE_MASTER']], check=True)
    query = args[args.index('--jq') + 1]
    for page in json.loads((root / 'pages.json').read_text()):
        subprocess.run(['jq', '-r', query], input=json.dumps(page), text=True, check=True)
elif args[:2] == ['pr', 'create']:
    values = dict(zip(args[2::2], args[3::2]))
    values['body'] = pathlib.Path(values['--body-file']).read_text()
    (root / 'created-pr.json').write_text(json.dumps(values))
    if os.environ.get('TEST_PR_FAIL'):
        sys.exit(1)
    print('https://github.com/example/repo/pull/42')
else:
    raise AssertionError(args)
'''

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='Sync test', GIT_AUTHOR_EMAIL='sync@example.test',
                        GIT_COMMITTER_NAME='Sync test', GIT_COMMITTER_EMAIL='sync@example.test',
                        TEST_ROOT=str(self.root), GH_REPO='example/repo', GITHUB_RUN_ID='123',
                        GITHUB_SERVER_URL='https://github.com', RUNNER_TEMP=str(self.root),
                        GITHUB_STEP_SUMMARY=str(self.root / 'summary.md'))
        self.remote = self.root / 'remote.git'
        self.seed = self.root / 'seed'
        self.worker = self.root / 'worker'
        self.git(self.root, 'init', '--bare', '--initial-branch=master', str(self.remote))
        self.git(self.root, 'init', '--initial-branch=master', str(self.seed))
        (self.seed / 'base.txt').write_text('base\n')
        self.git(self.seed, 'add', '.')
        self.git(self.seed, 'commit', '-m', 'base')
        self.base = self.git(self.seed, 'rev-parse', 'HEAD')
        self.git(self.seed, 'branch', 'dev')
        self.git(self.seed, 'remote', 'add', 'origin', str(self.remote))
        self.git(self.seed, 'push', 'origin', 'master', 'dev')
        self.git(self.root, 'clone', str(self.remote), str(self.worker))
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'gh').write_text(MOCK_GH)
        (bin_dir / 'gh').chmod(0o755)
        self.env['PATH'] = str(bin_dir) + os.pathsep + self.env['PATH']
        (self.root / 'pages.json').write_text('[[]]')

    def git(self, cwd, *args):
        return subprocess.run(['git', *args], cwd=cwd, env=self.env,
                              check=True, text=True, capture_output=True).stdout.strip()

    def commit(self, branch, filename):
        self.git(self.seed, 'switch', branch)
        (self.seed / filename).write_text(filename + '\n')
        self.git(self.seed, 'add', filename)
        self.git(self.seed, 'commit', '-m', filename)
        self.git(self.seed, 'push', 'origin', branch)
        return self.git(self.seed, 'rev-parse', 'HEAD')

    def refs(self):
        return self.git(self.remote, 'show-ref')

    def run_workflow(self, expected=0, **extra):
        result = subprocess.run(['bash', '-c', SCRIPT], cwd=self.worker,
                                env=dict(self.env, **extra), text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def calls(self):
        path = self.root / 'gh-calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def summary(self):
        path = self.root / 'summary.md'
        return path.read_text() if path.exists() else ''

    def assert_snapshot(self, sha, run_id='123'):
        self.assertEqual(self.git(self.remote, 'rev-parse', 'refs/heads/sync/master-to-dev-' + run_id), sha)

    def assert_pr(self, sha):
        pr = json.loads((self.root / 'created-pr.json').read_text())
        self.assertEqual(pr['--base'], 'dev')
        self.assertEqual(pr['--head'], 'sync/master-to-dev-123')
        self.assertEqual(pr['--title'], 'chore: sync master into dev')
        self.assertIn(sha, pr['body'])
        self.assertIn('**Merge this PR using Create a merge commit.**', pr['body'])
        self.assertIn('Approve\n  workflows to run', pr['body'])
        self.assertIn('https://github.com/example/repo/pull/42', self.summary())

    def test_uses_the_calling_repository_in_api_requests_and_links(self):
        self.env['GH_REPO'] = 'another-org/another-repo'
        master = self.commit('master', 'hotfix.txt')
        self.run_workflow()
        self.assert_snapshot(master)
        pr = json.loads((self.root / 'created-pr.json').read_text())
        self.assertIn(f'https://github.com/another-org/another-repo/commit/{master}', pr['body'])
        self.assertIn('https://github.com/another-org/another-repo/tree/', self.summary())

    def test_equal_or_dev_ahead_exits_before_github_calls(self):
        for advance in [False, True]:
            if advance:
                self.commit('dev', 'unreleased.txt')
            before = self.refs()
            self.run_workflow()
            self.assertEqual(self.refs(), before)
            self.assertEqual(self.calls(), [])
            self.assertIn('dev already contains master', self.summary())

    def test_dev_behind_creates_exact_snapshot_and_preserves_long_lived_refs(self):
        sha = self.commit('master', 'hotfix.txt')
        self.run_workflow()
        self.assert_snapshot(sha)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'master'), sha)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'dev'), self.base)
        self.assert_pr(sha)

    def test_diverged_branches_create_snapshot_and_merge_restores_ancestry(self):
        master = self.commit('master', 'hotfix.txt')
        dev = self.commit('dev', 'feature.txt')
        self.run_workflow()
        self.assert_snapshot(master)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'dev'), dev)
        self.git(self.seed, 'merge', '--no-ff', 'master', '-m', 'Sync master into dev')
        self.git(self.seed, 'push', 'origin', 'dev')
        count = len(self.calls())
        self.run_workflow()
        self.assertEqual(len(self.calls()), count)
        self.assertIn('dev already contains master', self.summary())

    def test_existing_sync_on_second_page_exits_with_link(self):
        self.commit('master', 'hotfix.txt')
        def pr(head, repo='example/repo', number=7):
            return {'head': {'ref': head, 'repo': {'full_name': repo}},
                    'base': {'repo': {'full_name': 'example/repo'}},
                    'html_url': f'https://github.com/example/repo/pull/{number}'}
        pages = [[pr('sync/master-to-dev-1', repo='fork/repo'), pr('feat/unrelated')],
                 [pr('sync/master-to-dev-2', number=99)]]
        (self.root / 'pages.json').write_text(json.dumps(pages))
        before = self.refs()
        self.run_workflow()
        self.assertEqual(self.refs(), before)
        self.assertIn('/pull/99', self.summary())
        self.assertNotIn('/pull/7', self.summary())
        pages = [[pr('master', number=100)]]
        (self.root / 'pages.json').write_text(json.dumps(pages))
        self.run_workflow()
        self.assertEqual(self.refs(), before)
        self.assertIn('/pull/100', self.summary())

    def test_fork_sync_does_not_block_creation(self):
        master = self.commit('master', 'hotfix.txt')
        (self.root / 'pages.json').write_text(json.dumps([[{
            'head': {'ref': 'sync/master-to-dev-1', 'repo': {'full_name': 'fork/repo'}},
            'base': {'repo': {'full_name': 'example/repo'}}, 'html_url': 'fork-pr-url'}]]))
        self.run_workflow()
        self.assert_snapshot(master)

    def test_api_failure_stops_before_any_ref_is_created(self):
        self.commit('master', 'hotfix.txt')
        before = self.refs()
        self.run_workflow(expected=1, TEST_API_FAIL='1')
        self.assertEqual(self.refs(), before)
        self.assertFalse((self.root / 'created-pr.json').exists())

    def test_failed_pr_can_be_retried_without_changing_snapshot(self):
        master = self.commit('master', 'hotfix.txt')
        self.run_workflow(expected=1, TEST_PR_FAIL='1')
        self.assert_snapshot(master)
        before = self.refs()
        self.run_workflow()
        self.assertEqual(self.refs(), before)
        self.assert_pr(master)

    def test_rerun_cannot_overwrite_conflict_resolution(self):
        master = self.commit('master', 'hotfix.txt')
        self.run_workflow(expected=1, TEST_PR_FAIL='1')
        self.git(self.seed, 'switch', '-c', 'sync/master-to-dev-123', master)
        changed = self.commit('sync/master-to-dev-123', 'resolution.txt')
        before = self.refs()
        result = self.run_workflow(expected=1)
        self.assertIn('already exists at a different commit', result.stdout)
        self.assertEqual(self.refs(), before)
        self.assert_snapshot(changed)

    def test_new_dispatch_can_snapshot_new_master_after_failed_old_run(self):
        old = self.commit('master', 'hotfix.txt')
        self.run_workflow(expected=1, TEST_PR_FAIL='1')
        latest = self.commit('master', 'new-hotfix.txt')
        self.run_workflow(expected=1)
        self.assert_snapshot(old)
        self.run_workflow(GITHUB_RUN_ID='124')
        self.assert_snapshot(latest, '124')
        self.assert_snapshot(old)

    def test_master_changes_after_fetch_do_not_change_snapshot(self):
        old = self.commit('master', 'hotfix.txt')
        latest = self.commit('master', 'new-hotfix.txt')
        self.git(self.remote, 'update-ref', 'refs/heads/master', old)
        self.run_workflow(TEST_ADVANCE_MASTER=latest)
        self.assert_snapshot(old)
        self.assertEqual(self.git(self.remote, 'rev-parse', 'master'), latest)
        self.assert_pr(old)

if __name__ == '__main__':
    unittest.main(verbosity=2)
