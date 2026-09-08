#!/usr/bin/env python3
"""Offline watcher checks; requires the same yq executable as the audit workflow."""

import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import mock_open, patch


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / "workflows/watch-dependency-graph.yml").read_text()
ENV = {"ORG": "ExampleOrg", "DRY_RUN": "false", "GH_TOKEN": "mock-token",
       "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "WATCHER_BOT": "dependency-watch[bot]"}
with patch.dict(os.environ, ENV, clear=True):
    spec = importlib.util.spec_from_file_location("dependency_watch", ROOT / "scripts/watch-dependency-graph.py")
    watch = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = watch
    spec.loader.exec_module(watch)
REAL_API, REAL_RUN = watch.api, subprocess.run
BOT = {"login": ENV["WATCHER_BOT"], "type": "Bot"}
HUMAN = {"login": "maintainer", "type": "User"}


class DependencyWatchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV, clear=True))
        self.enterContext(patch.object(watch, "DRY_RUN", False))
        self.api = self.enterContext(patch.object(watch, "api", side_effect=AssertionError("unexpected API call")))
        self.command = self.enterContext(patch.object(subprocess, "run", side_effect=self.local_yaml))
        self.stream = self.enterContext(patch("builtins.open", mock_open()))

    def local_yaml(self, command, **kwargs):
        self.assertEqual(command, ["yq", "-o=json", ".", "-"], "only local YAML parsing may run")
        return REAL_RUN(command, env={"PATH": ENV["PATH"]}, **kwargs)

    def replies(self, responses):
        pending = {key: list(values) for key, values in responses.items()}

        def respond(path, method="GET", body=None, **kwargs):
            key = (method, path)
            self.assertTrue(pending.get(key), f"unexpected or repeated API call: {key}")
            result = pending[key].pop(0)
            if isinstance(result, Exception):
                raise result
            return copy.deepcopy(result)

        self.api.side_effect = respond
        return pending

    def consumed(self, pending):
        self.assertFalse({key: values for key, values in pending.items() if values})

    def edge(self, consumer="Consumer", branch="main", path="Cargo.toml", dep="library",
             producer="Producer", pin="1.0.0", kind="exact"):
        return watch.Edge(consumer, branch, path, dep, producer, pin, kind)

    def release(self, version="v2.0.0"):
        return {"tag_name": version, "draft": False, "prerelease": False}

    def issue(self, *, state="open", target="v2.0.0", resolved=False, edges=(), dep="library"):
        issue = dict(watch.issue_payload(dep, "Producer", target, edges, resolved=resolved),
                     number=7, state=state, user=dict(BOT))
        if state == "closed":
            issue.update(state_reason="completed", closed_by=dict(BOT if resolved else HUMAN))
        return issue

    def test_semver_orders_prereleases_and_ignores_build_metadata(self):
        versions = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
                    "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0"]
        self.assertEqual(sorted(reversed(versions), key=watch.semver), versions)
        self.assertEqual(watch.semver("v1.2.3+build.4"), watch.semver("1.2.3+different"))
        for value in ("1.2", "01.2.3", "1.2.3-01", "1.2.3+", "^1.2.3", "latest", ""):
            with self.subTest(value=value):
                self.assertIsNone(watch.semver(value))

    def test_latest_uses_latest_release_and_rejects_drafts_prereleases_or_invalid_tags(self):
        path = "repos/ExampleOrg/Producer/releases/latest"
        cache = {}
        pending = self.replies({("GET", path): [self.release("v2.0.0+build.4")]})
        self.assertEqual(watch.latest_release("Producer", cache), "v2.0.0+build.4")
        self.assertEqual(watch.latest_release("Producer", cache), "v2.0.0+build.4")
        self.consumed(pending)
        self.assertEqual(self.api.call_count, 1)
        self.assertTrue(self.api.call_args.kwargs["missing"])
        for release in (dict(self.release(), draft=True), dict(self.release(), prerelease=True),
                        self.release("release-next"), [], {"tag_name": "v2.0.0"}):
            with self.subTest(release=release):
                self.replies({("GET", path): [release]})
                cache = {}
                with self.assertRaises(watch.APIError):
                    watch.latest_release("Producer", cache)
                self.assertEqual(cache, {})
        self.replies({("GET", path): [None]})
        self.assertIsNone(watch.latest_release("Producer", {}))

    def test_nested_cargo_honors_workspace_patches_and_exact_rev_keys(self):
        texts = {
            "Cargo.toml": '''[workspace]
members = ["crates/client"]
[workspace.dependencies]
library = { git = "https://github.com/ExampleOrg/Producer.git", tag = "v1.0.0" }
[patch."https://github.com/ExampleOrg/Producer"]
library = { path = "vendor/library" }
''',
            "crates/client/Cargo.toml": '''[package]
name = "client"
[dependencies]
inherited = { workspace = true }
alias = { package = "library", git = "https://github.com/ExampleOrg/Producer.git", tag = "v1.0.0" }
[dev-dependencies]
preview = { git = "https://github.com/ExampleOrg/Preview.git", tag = "v1.0.0-dev-preview" }
[build-dependencies]
revision = { git = "https://github.com/ExampleOrg/Builder.git", rev = "abcdef123456" }
[target.'cfg(unix)'.dependencies]
api = "=1.2.3"
''',
        }
        docs = {path: watch.parse_manifest(path, text) for path, text in texts.items()}
        root = watch.edges_from("Consumer", "main", "Cargo.toml", docs, {})
        nested = watch.edges_from("Consumer", "dev", "crates/client/Cargo.toml", docs, {"api": "API"})
        self.assertEqual([(e.dependency, e.kind) for e in root], [("library", "patched")])
        self.assertEqual({e.dependency: (e.pin, e.kind, e.producer) for e in nested}, {
            "alias": ("v1.0.0", "patched", "Producer"),
            "preview": ("v1.0.0-dev-preview", "exact", "Preview"),
            "revision": ("abcdef123456", "revision", "Builder"), "api": ("1.2.3", "exact", "API")})
        self.assertTrue(all(e.path == "crates/client/Cargo.toml" and e.branch == "dev" for e in nested))

    def test_local_cargo_paths_and_same_repo_workspace_edges_are_skipped(self):
        path = "Cargo.toml"
        doc = watch.parse_manifest(path, '''[workspace]
members = ["crates/client"]
[workspace.dependencies]
ambiguous = { path = "crates/ambiguous", version = "=1.0.0" }
shadowed = { path = "crates/shadowed", version = "=1.0.0" }
same_repo = { git = "https://github.com/ExampleOrg/Consumer.git", tag = "v1.0.0" }
external = { git = "https://github.com/ExampleOrg/Producer.git", tag = "v1.0.0" }
[dependencies]
inherited = { workspace = true }
renamed_local = { package = "ambiguous", path = "crates/ambiguous", version = "=1.0.0" }
''')
        edges = watch.edges_from("Consumer", "main", path, {path: doc},
                                 {"ambiguous": None, "shadowed": "Producer"})
        self.assertEqual(edges, [self.edge(dep="external", pin="v1.0.0")])

    def test_nested_npm_and_pubspec_parse_exact_ranges_and_revisions(self):
        path = "packages/web/package.json"
        doc = watch.parse_manifest(path, json.dumps({
            "dependencies": {"@bitcredit/ui-library": "^1.0.0", "alias": "github:ExampleOrg/ui#v1.0.0"},
            "devDependencies": {"ui": "=1.1.0"},
            "peerDependencies": {"branch": "github:ExampleOrg/ui#dev"},
            "optionalDependencies": {"revision": "git+https://github.com/ExampleOrg/ui.git#abcdef12"}}))
        npm = watch.edges_from("Consumer", "main", path, {path: doc}, dict(watch.NPM_OWNER, ui="ui"))
        self.assertEqual({e.dependency: (e.pin, e.kind) for e in npm}, {
            "@bitcredit/ui-library": ("^1.0.0", "range"), "alias": ("v1.0.0", "exact"),
            "ui": ("1.1.0", "exact"), "branch": ("dev", "range"), "revision": ("abcdef12", "revision")})
        path = "apps/mobile/pubspec.yaml"
        doc = watch.parse_manifest(path, '''name: mobile
dependencies:
  widget:
    git:
      url: https://github.com/ExampleOrg/Widget.git
      path: packages/widget
      ref: v1.2.3
  default_widget:
    git: https://github.com/ExampleOrg/Widget.git
dev_dependencies:
  revision_widget:
    git: {url: "https://github.com/ExampleOrg/Widget.git", ref: abcdef12}
''')
        dart = watch.edges_from("Consumer", "dev", path, {path: doc}, {})
        self.assertEqual({e.dependency: (e.pin, e.kind) for e in dart}, {
            "widget": ("v1.2.3", "exact"), "default_widget": ("default branch", "range"),
            "revision_widget": ("abcdef12", "revision")})
        for text in ("dependencies: [", "- not-a-mapping"):
            with self.subTest(yaml=text), self.assertRaises(ValueError):
                watch.parse_manifest(path, text)

    def test_collect_graph_reads_every_manifest_at_pinned_branch_shas(self):
        def blob(text):
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}

        consumer = {"name": "Consumer", "default_branch": "main", "archived": False, "fork": False}
        producer = dict(consumer, name="Producer")
        manifests = {"crates/client/Cargo.toml": '[dependencies]\nlibrary = "=1.0.0"\n',
                     "packages/web/package.json": '{"dependencies":{"library":"1.0.0"}}',
                     "apps/mobile/pubspec.yaml": 'dependencies:\n  library:\n    git: {url: "https://github.com/ExampleOrg/Producer.git", ref: v1.0.0}\n'}
        tree = [{"path": path, "type": "blob"} for path in manifests]
        tree += [{"path": "vendor/ignored/Cargo.toml", "type": "blob"},
                 {"path": "node_modules/ignored/package.json", "type": "blob"},
                 {"path": "submodule/Cargo.toml", "type": "commit"}]
        responses = {
            ("GET", "orgs/ExampleOrg/repos?per_page=100&page=1"):
                [[consumer, producer, dict(consumer, name="Archived", archived=True), dict(consumer, name="Fork", fork=True)]],
            ("GET", "repos/ExampleOrg/Consumer/branches/main"): [{"commit": {"sha": "1" * 40}}],
            ("GET", "repos/ExampleOrg/Consumer/branches/dev"): [{"commit": {"sha": "2" * 40}}],
            ("GET", "repos/ExampleOrg/Producer/branches/main"): [{"commit": {"sha": "3" * 40}}],
            ("GET", "repos/ExampleOrg/Producer/branches/dev"): [None],
            ("GET", f"repos/ExampleOrg/Consumer/git/trees/{'1' * 40}?recursive=1"):
                [{"truncated": False, "tree": tree}],
            ("GET", f"repos/ExampleOrg/Consumer/git/trees/{'2' * 40}?recursive=1"):
                [{"truncated": False, "tree": [{"path": "crates/client/Cargo.toml", "type": "blob"}]}],
            ("GET", f"repos/ExampleOrg/Producer/git/trees/{'3' * 40}?recursive=1"):
                [{"truncated": False, "tree": [{"path": "Cargo.toml", "type": "blob"}]}],
            ("GET", f"repos/ExampleOrg/Consumer/contents/crates/client/Cargo.toml?ref={'2' * 40}"):
                [blob(manifests["crates/client/Cargo.toml"])],
            ("GET", f"repos/ExampleOrg/Producer/contents/Cargo.toml?ref={'3' * 40}"):
                [blob('[package]\nname = "library"\n')],
        }
        responses.update({("GET", f"repos/ExampleOrg/Consumer/contents/{path}?ref={'1' * 40}"): [blob(text)]
                          for path, text in manifests.items()})
        pending = self.replies(responses)
        gaps, incomplete = [], set()
        repos, edges = watch.collect_graph(gaps, incomplete)
        self.assertEqual([r["name"] for r in repos], ["Consumer", "Producer"])
        self.assertEqual({(e.branch, e.path) for e in edges},
                         {("main", path) for path in manifests} | {("dev", "crates/client/Cargo.toml")})
        self.assertTrue(all(e.producer == "Producer" and e.kind == "exact" for e in edges))
        self.assertEqual((gaps, incomplete), ([], set()))
        self.consumed(pending)

    def test_truncated_trees_and_read_errors_leave_explicit_incomplete_gaps(self):
        for case in ("truncated", "tree error", "content error", "dev error"):
            with self.subTest(case=case):
                tree = {"truncated": case == "truncated", "tree": []}
                if case == "content error":
                    tree["tree"] = [{"path": "nested/Cargo.toml", "type": "blob"}]
                responses = {
                    ("GET", "orgs/ExampleOrg/repos?per_page=100&page=1"):
                        [[{"name": "Consumer", "default_branch": "main", "archived": False, "fork": False}]],
                    ("GET", "repos/ExampleOrg/Consumer/branches/main"): [{"commit": {"sha": "1" * 40}}],
                    ("GET", f"repos/ExampleOrg/Consumer/git/trees/{'1' * 40}?recursive=1"):
                        [watch.APIError("HTTP 500") if case == "tree error" else tree],
                    ("GET", "repos/ExampleOrg/Consumer/branches/dev"):
                        [watch.APIError("HTTP 403") if case == "dev error" else None],
                }
                if case == "content error":
                    responses["GET", f"repos/ExampleOrg/Consumer/contents/nested/Cargo.toml?ref={'1' * 40}"] = [watch.APIError("HTTP 403")]
                pending = self.replies(responses)
                gaps, incomplete = [], set()
                _, edges = watch.collect_graph(gaps, incomplete)
                self.assertEqual(edges, [])
                self.assertEqual(incomplete, {"Consumer"})
                self.assertTrue(gaps)
                self.assertTrue(all(gap.startswith("Consumer/") for gap in gaps))
                self.consumed(pending)

    def test_one_grouped_issue_contains_all_behind_locations_only(self):
        behind = [self.edge(), self.edge(branch="dev", path="crates/client/Cargo.toml"),
                  self.edge(path="apps/mobile/pubspec.yaml", pin="v1.5.0")]
        edges = behind + [self.edge(path="current/Cargo.toml", pin="2.0.0"),
                          self.edge(path="range/Cargo.toml", kind="range"), self.edge(path="patched/Cargo.toml", kind="patched")]
        self.replies({("GET", "repos/ExampleOrg/Producer/releases/latest"): [self.release()]})
        gaps, incomplete = [], set()
        actions = watch.plan_actions(edges, {"Consumer": []}, gaps, incomplete)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["kind"], "open")
        body = actions[0]["payload"]["body"]
        for edge in behind:
            self.assertIn(f"| {edge.branch} | [{edge.path}]", body)
        for path in ("current/Cargo.toml", "range/Cargo.toml", "patched/Cargo.toml"):
            self.assertNotIn(path, body)
        self.assertEqual((gaps, incomplete), ([], set()))

    def test_manual_close_suppresses_only_the_same_semver_target(self):
        closed = self.issue(state="closed", target="v2.0.0+old-build")
        for target, expected in (("v2.0.0+new-build", 0), ("v2.0.1", 1)):
            with self.subTest(target=target):
                self.replies({("GET", "repos/ExampleOrg/Producer/releases/latest"): [self.release(target)]})
                gaps = []
                actions = watch.plan_actions([self.edge()], {"Consumer": [closed]}, gaps, set())
                self.assertEqual(len(actions), expected)
                self.assertEqual(gaps, [])
                if actions:
                    self.assertEqual(actions[0]["kind"], "open")

    def test_complete_scan_closes_caught_up_or_no_longer_exact_dependencies(self):
        for edge in (self.edge(pin="v2.0.0"), self.edge(pin="^2.0.0", kind="range")):
            with self.subTest(kind=edge.kind):
                self.replies({("GET", "repos/ExampleOrg/Producer/releases/latest"): [self.release()]} if edge.kind == "exact" else {})
                edges = [edge]
                actions = watch.plan_actions(edges, {"Consumer": [self.issue()]}, [], set())
                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0]["kind"], "close")
                self.assertEqual(actions[0]["payload"]["state"], "closed")
                closed = dict(actions[0]["payload"], number=7, user=BOT, closed_by=BOT)
                self.assertTrue(watch.issue_state(closed)["resolved"])

    def test_unmapped_dependency_leaves_the_issue_unchanged_with_a_gap(self):
        # Ownership can disappear when a producer is archived, renamed, or removed.
        path = "Cargo.toml"
        doc = watch.parse_manifest(path, '[dependencies]\nlibrary = "=1.0.0"\n')
        edges = watch.edges_from("Consumer", "main", path, {path: doc}, {})
        gaps, incomplete = [], set()
        self.assertEqual(watch.plan_actions(edges, {"Consumer": [self.issue()]}, gaps, incomplete), [])
        self.assertIn("previous dependency is no longer mapped; issue unchanged", gaps[0])
        self.assertEqual(incomplete, {"Consumer"})
        self.api.assert_not_called()

    def test_incomplete_scan_prevents_closure_and_discards_earlier_planned_actions(self):
        issues = {"Consumer": [self.issue()]}
        self.assertEqual(watch.plan_actions([], issues, [], {"Consumer"}), [])
        self.api.assert_not_called()
        self.replies({("GET", "repos/ExampleOrg/Broken/releases/latest"): [watch.APIError("HTTP 403")]})
        gaps, incomplete = [], set()
        # The mapped range would close before the later dependency read fails.
        edges = [self.edge(kind="range", pin="^2.0.0"), self.edge(dep="zzz", producer="Broken")]
        actions = watch.plan_actions(edges, issues, gaps, incomplete)
        self.assertEqual(actions, [])
        self.assertEqual(incomplete, {"Consumer"})
        self.assertIn("HTTP 403", gaps[0])

    def test_incomplete_producer_cannot_make_a_consumer_dependency_look_removed(self):
        consumer = {"name": "Consumer", "default_branch": "main", "archived": False, "fork": False}
        manifest = base64.b64encode(b'[dependencies]\nlibrary = "=1.0.0"\n').decode()
        self.replies({
            ("GET", "orgs/ExampleOrg/repos?per_page=100&page=1"): [[consumer, dict(consumer, name="Producer")]],
            ("GET", "repos/ExampleOrg/Consumer/branches/main"): [{"commit": {"sha": "1" * 40}}],
            ("GET", f"repos/ExampleOrg/Consumer/git/trees/{'1' * 40}?recursive=1"):
                [{"truncated": False, "tree": [{"path": "Cargo.toml", "type": "blob"}]}],
            ("GET", f"repos/ExampleOrg/Consumer/contents/Cargo.toml?ref={'1' * 40}"):
                [{"encoding": "base64", "content": manifest}],
            ("GET", "repos/ExampleOrg/Consumer/branches/dev"): [None],
            ("GET", "repos/ExampleOrg/Producer/branches/main"): [{"commit": {"sha": "2" * 40}}],
            ("GET", f"repos/ExampleOrg/Producer/git/trees/{'2' * 40}?recursive=1"): [watch.APIError("HTTP 403")],
            ("GET", "repos/ExampleOrg/Producer/branches/dev"): [None],
        })
        gaps, incomplete = [], set()
        _, edges = watch.collect_graph(gaps, incomplete)
        self.assertIn("Producer", incomplete)
        self.assertTrue(gaps)
        actions = watch.plan_actions(edges, {"Consumer": [self.issue()], "Producer": []}, gaps, incomplete)
        self.assertEqual(actions, [], "a failed producer read is not proof that the consumer removed its pin")

    def test_automatically_closed_issue_reopens_after_regression(self):
        closed = self.issue(state="closed", resolved=True)
        for metadata in ({"closed_by": dict(BOT, login="another-app[bot]")},
                         {"user": HUMAN}, {"state_reason": "not_planned"}):
            with self.subTest(metadata=metadata):
                self.assertFalse(watch.issue_state(dict(closed, **metadata))["resolved"])
        self.replies({("GET", "repos/ExampleOrg/Producer/releases/latest"): [self.release()]})
        actions = watch.plan_actions([self.edge()], {"Consumer": [closed]}, [], set())
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual((action["kind"], action["existing"]["number"], action["payload"]["state"]), ("update", 7, "open"))
        self.assertFalse(watch.issue_state(action["payload"])["resolved"])
        pending = self.replies({
            ("PATCH", "repos/ExampleOrg/Consumer/issues/7"): [{"number": 7}],
            ("GET", "repos/ExampleOrg/Consumer/issues/7"): [dict(action["payload"], number=7, user=BOT)],
        })
        self.assertEqual(watch.apply_action(action), "update verified: #7")
        self.consumed(pending)

    def test_main_reads_native_closure_before_respecting_a_human_reclose(self):
        self.enterContext(patch.object(watch, "collect_graph", return_value=([{"name": "Consumer"}], [self.edge()])))
        for reason in ("not_planned", "completed"):
            with self.subTest(state_reason=reason):
                # A human reclosed an issue whose old bot-written body still says resolved=true.
                detail = dict(self.issue(state="closed", resolved=True), closed_by=HUMAN, state_reason=reason)
                listed = {key: value for key, value in detail.items() if key not in ("closed_by", "state_reason")}
                self.api.reset_mock()
                pending = self.replies({
                    ("GET", "repos/ExampleOrg/Consumer/issues?state=all&per_page=100&page=1"): [[listed]],
                    ("GET", "repos/ExampleOrg/Consumer/issues/7"): [detail],
                    ("GET", "repos/ExampleOrg/Producer/releases/latest"): [self.release()],
                })
                self.assertEqual(watch.main(), 0)
                self.consumed(pending)
                self.assertEqual([c.args[0] for c in self.api.call_args_list], [
                    "repos/ExampleOrg/Consumer/issues?state=all&per_page=100&page=1",
                    "repos/ExampleOrg/Consumer/issues/7", "repos/ExampleOrg/Producer/releases/latest"])
                self.assertTrue(all(len(c.args) == 1 for c in self.api.call_args_list))

    def test_missing_closure_attribution_cannot_reopen_an_issue(self):
        for field in ("closed_by", "user"):
            with self.subTest(missing=field):
                closed = self.issue(state="closed", resolved=True)
                closed.pop(field)
                self.replies({("GET", "repos/ExampleOrg/Producer/releases/latest"): [self.release()]})
                gaps, incomplete = [], set()
                self.assertEqual(watch.plan_actions([self.edge()], {"Consumer": [closed]}, gaps, incomplete), [])
                self.assertIn("last closure could not be attributed", gaps[0])
                self.assertEqual(incomplete, {"Consumer"})

    def test_main_ignores_copied_markers_from_humans_other_bots_and_prs(self):
        self.enterContext(patch.object(watch, "collect_graph", return_value=([{"name": "Consumer"}], [self.edge()])))
        self.assertTrue(watch.own_issue(self.issue()))
        payload = watch.issue_payload("library", "Producer", "v2.0.0", [self.edge()])
        for author in (HUMAN, dict(BOT, login="another-app[bot]")):
            for state in ("open", "closed"):
                with self.subTest(author=author["login"], state=state):
                    copied = dict(self.issue(state=state), user=author)
                    pull_request = dict(self.issue(), number=8, pull_request={})
                    self.assertFalse(watch.own_issue(copied))
                    self.assertFalse(watch.own_issue(pull_request))
                    self.api.reset_mock()
                    pending = self.replies({
                        ("GET", "repos/ExampleOrg/Consumer/issues?state=all&per_page=100&page=1"):
                            [[copied, pull_request]],
                        ("GET", "repos/ExampleOrg/Producer/releases/latest"): [self.release()],
                        ("POST", "repos/ExampleOrg/Consumer/issues"): [{"number": 9}],
                        ("GET", "repos/ExampleOrg/Consumer/issues/9"): [dict(payload, number=9, state="open", user=BOT)],
                    })
                    self.assertEqual(watch.main(), 0)
                    self.consumed(pending)
                    writes = [c.args[:2] for c in self.api.call_args_list if len(c.args) > 1]
                    self.assertEqual(writes, [("repos/ExampleOrg/Consumer/issues", "POST")])

    def test_identical_open_issue_is_not_updated_again(self):
        edges = [self.edge(), self.edge(branch="dev", path="nested/Cargo.toml")]
        self.replies({("GET", "repos/ExampleOrg/Producer/releases/latest"): [self.release()]})
        self.assertEqual(watch.plan_actions(edges, {"Consumer": [self.issue(edges=edges)]}, [], set()), [])

    def test_uncertain_creation_rereads_without_a_second_post(self):
        payload = watch.issue_payload("library", "Producer", "v2.0.0", [self.edge()])
        action = dict(repo="Consumer", dep="library", kind="open", existing=None, payload=payload)
        issue = dict(payload, number=7, state="open", user=BOT)
        for count in (0, 1, 2):
            with self.subTest(matches=count):
                self.api.reset_mock()
                responses = {
                    ("POST", "repos/ExampleOrg/Consumer/issues"): [subprocess.TimeoutExpired("mock gh", 60)],
                    ("GET", "repos/ExampleOrg/Consumer/issues?state=all&per_page=100&page=1"):
                        [[dict(issue, number=7 + n) for n in range(count)]],
                }
                if count == 1:
                    responses["GET", "repos/ExampleOrg/Consumer/issues/7"] = [issue]
                pending = self.replies(responses)
                if count == 1:
                    self.assertEqual(watch.apply_action(action), "open verified: #7")
                else:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        watch.apply_action(action)
                self.consumed(pending)
                self.assertEqual(sum(c.args[1:2] == ("POST",) for c in self.api.call_args_list), 1)

    def test_main_reads_all_issue_pages_before_writes_and_stops_on_prefetch_failure(self):
        edges = [self.edge(), self.edge(consumer="Other")]
        self.enterContext(patch.object(watch, "collect_graph", return_value=([{"name": "Consumer"}, {"name": "Other"}], edges)))
        for mode in ("write", "dry", "read failure"):
            with self.subTest(mode=mode):
                watch.DRY_RUN = mode == "dry"
                self.api.reset_mock()
                responses = {
                    ("GET", "repos/ExampleOrg/Consumer/issues?state=all&per_page=100&page=1"):
                        [[{"number": n, "pull_request": {}} for n in range(100)]],
                    ("GET", "repos/ExampleOrg/Consumer/issues?state=all&per_page=100&page=2"): [[]],
                    ("GET", "repos/ExampleOrg/Other/issues?state=all&per_page=100&page=1"):
                        [watch.APIError("HTTP 403") if mode == "read failure" else []],
                }
                if mode != "read failure":
                    responses["GET", "repos/ExampleOrg/Producer/releases/latest"] = [self.release()]
                if mode == "write":
                    for number, edge in enumerate(edges, 1):
                        payload = watch.issue_payload("library", "Producer", "v2.0.0", [edge])
                        responses["POST", f"repos/ExampleOrg/{edge.consumer}/issues"] = [{"number": number}]
                        responses["GET", f"repos/ExampleOrg/{edge.consumer}/issues/{number}"] = [dict(payload, state="open", user=BOT)]
                pending = self.replies(responses)
                self.assertEqual(watch.main(), 1 if mode == "read failure" else 0)
                self.consumed(pending)
                writes = [i for i, c in enumerate(self.api.call_args_list) if len(c.args) > 1]
                if mode == "write":
                    reads = [i for i, c in enumerate(self.api.call_args_list) if "/issues?state=all" in c.args[0]]
                    self.assertEqual(len(writes), 2)
                    self.assertLess(max(reads), min(writes))
                else:
                    self.assertEqual(writes, [])

    def test_api_distinguishes_missing_from_errors_and_dry_run_refuses_writes(self):
        self.command.side_effect = None
        for status in (404, 403, 500, 502):
            with self.subTest(status=status):
                self.command.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr=f"gh: failed (HTTP {status})")
                if status == 404:
                    self.assertIsNone(REAL_API("mock/path", missing=True))
                    with self.assertRaises(watch.APIError):
                        REAL_API("mock/path")
                else:
                    with self.assertRaises(watch.APIError):
                        REAL_API("mock/path", missing=True)
        self.command.return_value = subprocess.CompletedProcess([], 0, stdout="not JSON", stderr="")
        with self.assertRaisesRegex(watch.APIError, "invalid JSON"):
            REAL_API("mock/path")
        watch.DRY_RUN = True
        self.command.reset_mock()
        for method in ("POST", "PATCH", "DELETE", "PUT"):
            with self.subTest(method=method), self.assertRaisesRegex(watch.APIError, "dry-run"):
                REAL_API("mock/path", method, {})
        self.command.assert_not_called()

    def test_workflow_limits_pr_tokens_and_keeps_scheduler_writes_opt_in(self):
        workflow = watch.parse_manifest("workflow.yaml", WORKFLOW)
        self.assertEqual(workflow["permissions"], {})
        test_job, watch_job = workflow["jobs"]["test"], workflow["jobs"]["watch"]
        self.assertEqual(test_job["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.", json.dumps(test_job))
        self.assertIn({"run": "python3 .github/scripts/test_dependency_watch.py"}, test_job["steps"])
        self.assertEqual(watch_job["if"], "github.event_name != 'pull_request'")
        self.assertEqual(watch_job["needs"], "test")
        self.assertTrue(workflow["on"]["workflow_dispatch"]["inputs"]["dry_run"]["default"])
        self.assertEqual(watch_job["env"]["DRY_RUN"],
                         "${{ ((github.event_name == 'workflow_dispatch' && inputs.dry_run == false) || "
                         "(github.event_name == 'schedule' && vars.DEPENDENCY_WATCH_ENABLED == 'true')) && 'false' || 'true' }}")
        token = next(step for step in watch_job["steps"] if step.get("id") == "token")["with"]
        self.assertEqual(token["permission-contents"], "read")
        self.assertEqual(token["permission-issues"], "${{ env.DRY_RUN == 'true' && 'read' || 'write' }}")
        run = next(step for step in watch_job["steps"] if "run" in step)
        self.assertEqual(run["env"]["WATCHER_BOT"], "${{ steps.token.outputs.app-slug }}[bot]")


if __name__ == "__main__":
    unittest.main()
