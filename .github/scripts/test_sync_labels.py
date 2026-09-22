#!/usr/bin/env python3
"""Run the real label sync against a fake gh; no network and no write access."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


# Mutates its label state on a write, which is what makes the dry run and the write run
# comparable: a rename the write run performs is visible to the passes that follow it.
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, urllib.parse
args = sys.argv[1:]
state = json.load(open(os.environ['LABEL_STATE']))
# The route is the one argument naming an endpoint; -X takes a bare verb and -f a pair.
path = next(a for a in args[1:] if a.startswith(('repos/', 'orgs/')))
route = urllib.parse.unquote(path.split('?')[0])
repo = route.split('/')[2] if route.startswith('repos/') else None
if '-X' in args or '--method' in args:
    if os.environ['DRY_RUN'] == 'true':
        sys.exit('unexpected write in a dry run')
    with open(os.environ['LABEL_WRITES'], 'a') as out:
        out.write(json.dumps(args) + '\n')
    fields = dict(args[i + 1].split('=', 1) for i, a in enumerate(args) if a == '-f')
    labels = state[repo]
    if route.endswith('/labels'):
        labels.append(dict(name=fields['name'], color=fields['color'],
                           description=fields.get('description', '')))
    else:
        name = route.rsplit('/', 1)[-1]
        label = next(item for item in labels if item['name'] == name)
        label['name'] = fields.get('new_name', label['name'])
        label['color'] = fields.get('color', label['color'])
        label['description'] = fields.get('description', label['description'])
    json.dump(state, open(os.environ['LABEL_STATE'], 'w'))
    print('{"id":1}')
    sys.exit(0)
if route == 'orgs/%s/repos' % os.environ['ORG']:
    data = [dict(name=name, archived=False) for name in state]
elif route.endswith('/labels'):
    data = state[repo]
else:
    sys.exit('unhandled fixture endpoint: ' + route)
if '--slurp' in args:
    data = [data]
if '--jq' in args:
    result = subprocess.run(['jq', '-r', args[args.index('--jq') + 1]],
                            input=json.dumps(data), text=True)
    sys.exit(result.returncode)
print(json.dumps(data))
'''

SCRIPT = Path(__file__).with_name('sync-labels.sh')
NEWCOMERS = {'name': 'good first issue', 'color': '7057ff', 'description': 'Good for newcomers'}


class SyncLabelsTests(unittest.TestCase):
    def sync(self, manifest, labels, *, dry_run, expect_failure=False):
        """Return the action lines the run reports, and the writes it performed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh = root / 'gh'
            gh.write_text(FAKE_GH)
            gh.chmod(0o755)
            (root / 'manifest.json').write_text(json.dumps(manifest))
            (root / 'state.json').write_text(json.dumps({'demo': labels}))
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       ORG='Fixture', GH_TOKEN='offline-fixture', DRY_RUN=str(dry_run).lower(),
                       MANIFEST_JSON=str(root / 'manifest.json'),
                       GITHUB_STEP_SUMMARY=str(root / 'summary'),
                       LABEL_STATE=str(root / 'state.json'), LABEL_WRITES=str(root / 'writes'))
            result = subprocess.run(['bash', str(SCRIPT)], env=env, text=True,
                                    capture_output=True, timeout=45)
            if expect_failure:
                self.assertNotEqual(result.returncode, 0, result.stdout)
            else:
                self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
            self.assertNotIn('unhandled fixture', result.stderr)
            actions = [line.strip() for line in result.stdout.splitlines() if line.startswith('  ')]
            writes = (root / 'writes').read_text().splitlines() if (root / 'writes').exists() else []
            return actions, writes, result.stderr

    def test_dry_run_plans_exactly_what_the_write_run_performs(self):
        """The dry run is the pre-flight check for a manifest edit, so it has to be true."""
        manifest = {'renames': {'good first contribution': 'good first issue'},
                    'required': [NEWCOMERS], 'managed': []}
        stale = [dict(NEWCOMERS, name='good first contribution')]
        planned, dry_writes, _ = self.sync(manifest, stale, dry_run=True)
        performed, writes, _ = self.sync(manifest, stale, dry_run=False)
        self.assertEqual(planned, ["demo: rename 'good first contribution' -> 'good first issue'"])
        self.assertEqual(planned, performed)
        self.assertEqual(dry_writes, [])
        self.assertEqual(len(writes), 1)

    def test_a_colour_that_is_not_a_hex_string_stops_the_run(self):
        """`008672` unquoted parses as 8672.0, which would be pushed to every repository."""
        manifest = {'required': [dict(NEWCOMERS, color=8672.0)], 'managed': []}
        actions, writes, stderr = self.sync(manifest, [], dry_run=False, expect_failure=True)
        self.assertIn('colour values must be quoted six-digit hex strings', stderr)
        self.assertEqual((actions, writes), ([], []))

    def test_managed_labels_are_corrected_but_never_created(self):
        manifest = {'required': [], 'managed': [NEWCOMERS, {'name': 'design', 'color': 'd4c5f9'}]}
        drifted = [dict(NEWCOMERS, color='000000')]
        actions, writes, _ = self.sync(manifest, drifted, dry_run=False)
        self.assertEqual(actions, ["demo: fix 'good first issue' (colour 000000 -> 7057ff)"])
        self.assertEqual(len(writes), 1)


if __name__ == '__main__':
    unittest.main()
