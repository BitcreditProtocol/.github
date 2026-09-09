#!/usr/bin/env python3
"""Offline GitHub simulations; no external command or publication is permitted."""

import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("minimum", ROOT / "propose-wallet-minimum.py")
minimum = importlib.util.module_from_spec(spec)
spec.loader.exec_module(minimum)
ENV = {"ORG": "ExampleOrg", "AUTOMATION_BOT": "example-automation[bot]",
       "TARGET_ENV": "prod", "SOURCE_RELEASE_TAG": "v1.3.0", "PROPOSED_MIN_VERSION": "1.3.0",
       "READ_TOKEN": "fixture-read", "WRITE_TOKEN": "fixture-write", "DRY_RUN": "false"}


def blob(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class GitHub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = "a" * 40
        self.source = "9" * 40
        self.bot = {"type": "Bot", "login": cfg["AUTOMATION_BOT"]}
        self.refs = {"master": self.base}
        self.blobs = {}
        files = {}
        for env in ("dev", "staging", "prod"):
            data = b'{"min_supported_version": "1.2.0"}\n'
            self.blobs[blob(data)] = data
            files[f"static/wallet/min-version/{env}/min-supported-version.json"] = blob(data)
        self.blobs[blob(b"unrelated\n")] = b"unrelated\n"
        files["README.md"] = blob(b"unrelated\n")
        tree = self.tree(files)
        self.commits = {self.base: {"sha": self.base, "author": {"login": "owner", "type": "User"},
            "committer": {"login": "owner", "type": "User"}, "commit": {"message": "Baseline", "tree": {"sha": tree}},
            "parents": [], "files": []}}
        self.prs = []
        self.calls = []
        self.fail_read = None
        self.lost = None
        self.reject = None
        self.after_ref = None
        self.release = {"id": 42, "tag_name": cfg["SOURCE_RELEASE_TAG"], "draft": False,
                        "prerelease": False, "published_at": "2026-09-01T12:00:00Z", "body": "PRIVATE CHANGELOG DO NOT PUBLISH"}
        self.tags = {}
        self.tag_object = {"type": "commit", "sha": self.source}

    def tree(self, files):
        if not hasattr(self, "trees"):
            self.trees = {}
        value = hashlib.sha1(json.dumps(files, sort_keys=True).encode()).hexdigest()
        self.trees[value] = dict(files)
        return value

    def files(self, commit):
        return self.trees[self.commits[commit]["commit"]["tree"]["sha"]]

    @property
    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def ancestors(self, commit):
        result = [commit]
        while self.commits[commit]["parents"]:
            commit = self.commits[commit]["parents"][0]["sha"]
            result.append(commit)
        return result

    def api(self, cfg, endpoint, method="GET", body=None):
        self.calls.append((method, endpoint, copy.deepcopy(body)))
        if method == "GET" and self.fail_read and self.fail_read in endpoint:
            raise minimum.ProposalError("simulated read failure")
        if method != "GET" and self.reject and self.reject in endpoint:
            raise minimum.ProposalError("simulated rejected write")
        result = self.respond(cfg, endpoint, method, body)
        if method != "GET" and self.lost and self.lost in endpoint:
            self.lost = None
            raise minimum.ProposalError("simulated response lost after write")
        return copy.deepcopy(result)

    def respond(self, cfg, endpoint, method, body):
        path, query = endpoint.split("?", 1) if "?" in endpoint else (endpoint, "")
        args = parse_qs(query)
        wallet = f"repos/{cfg['ORG']}/wallet"
        root = cfg["root"]
        if method == "GET" and path.startswith(wallet + "/releases/tags/"):
            return self.release
        if method == "GET" and path.startswith(wallet + "/git/ref/tags/"):
            return {"ref": "refs/tags/" + cfg["SOURCE_RELEASE_TAG"], "object": self.tag_object}
        if method == "GET" and path.startswith(wallet + "/git/tags/"):
            value = path.rsplit("/", 1)[1]
            return {"sha": value, "object": self.tags[value]}
        if method == "GET" and path == root:
            return {"default_branch": "master"}
        if method == "GET" and path.startswith(root + "/git/ref/heads/"):
            branch = unquote(path.split("/heads/", 1)[1])
            return {"ref": "refs/heads/" + branch, "object": {"type": "commit", "sha": self.refs[branch]}}
        if method == "GET" and "/git/matching-refs/" in path:
            return [{"ref": "refs/heads/" + cfg["branch"], "object": {"type": "commit", "sha": self.refs[cfg["branch"]]}}] if cfg["branch"] in self.refs else []
        if method == "GET" and path.startswith(root + "/contents/"):
            name = path.split("/contents/", 1)[1]
            value = self.files(args["ref"][0])[name]
            return {"type": "file", "path": name, "encoding": "base64", "sha": value,
                    "content": base64.b64encode(self.blobs[value]).decode()}
        if method == "GET" and path.startswith(root + "/commits/"):
            return self.commits[path.rsplit("/", 1)[1]]
        if method == "GET" and path.startswith(root + "/git/commits/"):
            value = path.rsplit("/", 1)[1]
            return {"sha": value, "tree": self.commits[value]["commit"]["tree"]}
        if method == "GET" and "/compare/" in path:
            left, right = path.rsplit("/", 1)[1].split("...")
            left_chain, right_chain = self.ancestors(left), self.ancestors(right)
            state = "identical" if left == right else "ahead" if left in right_chain else "behind" if right in left_chain else "diverged"
            common = next(s for s in left_chain if s in right_chain)
            return {"status": state, "merge_base_commit": {"sha": common}}
        if method == "GET" and path == root + "/pulls":
            rows = copy.deepcopy(self.prs)
            for pr in rows:
                if pr["state"] == "open":
                    pr["head"]["sha"] = self.refs.get(cfg["branch"])
            page = int(args.get("page", ["1"])[0])
            return rows[(page-1)*100:page*100]
        if method == "GET" and re.search(r"/pulls/[0-9]+/files$", path):
            head = self.refs[cfg["branch"]]
            ancestor = next(s for s in self.ancestors(head) if s in self.ancestors(self.refs["master"]))
            return [{"filename": p, "status": "modified"} for p, value in self.files(head).items() if self.files(ancestor).get(p) != value]
        if method == "POST" and path == root + "/git/blobs":
            data = base64.b64decode(body["content"])
            value = blob(data)
            self.blobs[value] = data
            return {"sha": value}
        if method == "GET" and path.startswith(root + "/git/blobs/"):
            value = path.rsplit("/", 1)[1]
            if value not in self.blobs:
                raise minimum.ProposalError("missing blob")
            return {"sha": value, "encoding": "base64", "content": base64.b64encode(self.blobs[value]).decode()}
        if method == "POST" and path == root + "/git/trees":
            files = dict(self.trees[body["base_tree"]])
            assert len(body["tree"]) == 1 and body["tree"][0]["path"] == cfg["path"]
            files[cfg["path"]] = body["tree"][0]["sha"]
            return {"sha": self.tree(files)}
        if method == "POST" and path == root + "/git/commits":
            value = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
            parent = body["parents"][0]
            files = [{"filename": p, "status": "modified"} for p, v in self.trees[body["tree"]].items() if self.files(parent).get(p) != v]
            self.commits[value] = {"sha": value, "author": self.bot, "committer": self.bot,
                "commit": {"message": body["message"], "tree": {"sha": body["tree"]}, "verification": {"verified":True,"reason":"valid"}},
                "parents": [{"sha": parent}], "files": files}
            return {"sha": value}
        if method == "POST" and path == root + "/git/refs":
            branch = body["ref"].removeprefix("refs/heads/")
            assert branch == cfg["branch"] and branch != "master"
            if branch in self.refs:
                raise minimum.ProposalError("branch already exists")
            self.refs[branch] = body["sha"]
            if self.after_ref:
                self.after_ref()
            return {"object": {"sha": body["sha"]}}
        if method == "PATCH" and "/git/refs/heads/" in path:
            branch = path.split("/heads/", 1)[1]
            assert branch == cfg["branch"] and branch != "master" and body["force"] is False
            if self.refs[branch] not in self.ancestors(body["sha"]):
                raise minimum.ProposalError("not a fast-forward")
            self.refs[branch] = body["sha"]
            if self.after_ref:
                self.after_ref()
            return {"object": {"sha": body["sha"]}}
        if method == "POST" and path == root + "/pulls":
            assert body["head"] == cfg["branch"] and body["base"] == "master" and body["draft"] is True
            pr = {"number": len(self.prs)+1, "state": "open", "merged_at": None, "user": self.bot,
                  "head": {"ref": cfg["branch"], "sha": self.refs[cfg["branch"]], "repo": {"full_name": cfg['ORG']+'/static-assets'}},
                  "base": {"ref": "master", "repo": {"full_name": cfg['ORG']+'/static-assets'}},
                  "title": body["title"], "body": body["body"], "draft": True}
            self.prs.append(pr)
            return pr
        if method == "PATCH" and re.search(r"/pulls/[0-9]+$", path):
            pr = next(p for p in self.prs if p["number"] == int(path.rsplit("/", 1)[1]))
            assert set(body)=={"title", "body"}
            pr.update(body)
            return pr
        raise AssertionError((method, endpoint, body))


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV, clear=True))
        self.cfg = minimum.settings()
        self.remote = GitHub(self.cfg)
        self.enterContext(patch.object(minimum, "api", side_effect=self.remote.api))
        self.enterContext(patch.object(subprocess, "run", side_effect=AssertionError("external execution forbidden")))

    def run_proposal(self):
        return minimum.propose(self.cfg)

    def next_request(self):
        self.cfg["SOURCE_RELEASE_TAG"] = "v1.4.0"
        self.cfg["PROPOSED_MIN_VERSION"] = "1.4.0"
        self.remote.release.update(id=43, tag_name="v1.4.0")

    def test_create_same_request_noop_and_only_selected_environment_changes(self):
        original = dict(self.remote.files(self.remote.base))
        self.assertEqual(self.run_proposal()["status"], "proposed")
        before = list(self.remote.writes)
        self.assertEqual(self.run_proposal()["status"], "no-op")
        self.assertEqual(self.remote.writes, before)
        self.assertEqual(self.remote.refs["master"], self.remote.base)
        head = self.remote.refs[self.cfg["branch"]]
        self.assertEqual({p for p,v in self.remote.files(head).items() if original[p]!=v}, {self.cfg["path"]})
        self.assertEqual(len(self.remote.prs), 1)
        self.assertNotIn("PRIVATE CHANGELOG", self.remote.prs[0]["body"])
        self.assertIn("Google Play and the Apple App Store", self.remote.prs[0]["body"])

    def test_update_uses_same_pr_and_nonforce_fast_forward(self):
        self.run_proposal(); old = self.remote.refs[self.cfg["branch"]]
        self.next_request(); self.run_proposal()
        new = self.remote.refs[self.cfg["branch"]]
        self.assertIn(old, self.remote.ancestors(new))
        self.assertEqual(len(self.remote.prs), 1)
        self.assertIn("1.4.0", self.remote.prs[0]["title"])

    def test_new_request_after_merge_uses_current_default_without_force(self):
        self.run_proposal()
        merged=self.remote.refs[self.cfg["branch"]]
        self.remote.refs["master"]=merged
        self.remote.prs[0].update(state="closed",merged_at="2026-09-02T12:00:00Z")
        self.next_request();self.run_proposal()
        self.assertEqual(self.remote.refs["master"],merged)
        self.assertEqual(sum(p["state"]=="open" for p in self.remote.prs),1)
        state=minimum.state_of(self.remote.prs[-1]["body"],self.cfg)
        self.assertEqual(state["base_sha"],merged)
        self.assertEqual(state["base_version"],"1.3.0")

    def test_changed_default_floor_blocks_stale_pending_proposal(self):
        self.run_proposal();before=len(self.remote.writes)
        data=b'{"min_supported_version":"1.2.5"}\n'
        self.remote.blobs[blob(data)]=data
        files=dict(self.remote.files(self.remote.base));files[self.cfg["path"]]=blob(data)
        current="d"*40
        self.remote.commits[current]={"sha":current,"author":{"type":"User","login":"owner"},
            "committer":{"type":"User","login":"owner"},"commit":{"message":"Manual floor change","tree":{"sha":self.remote.tree(files)}},
            "parents":[{"sha":self.remote.base}],"files":[{"filename":self.cfg["path"],"status":"modified"}]}
        self.remote.refs["master"]=current
        self.next_request()
        with self.assertRaises(minimum.ProposalError):self.run_proposal()
        self.assertEqual(len(self.remote.writes),before)
        self.assertEqual(self.remote.refs["master"],current)

    def test_dry_run_defaults_safe_without_write_token(self):
        os.environ.pop("DRY_RUN"); os.environ.pop("WRITE_TOKEN")
        self.cfg = minimum.settings()
        self.assertEqual(self.run_proposal()["status"], "dry-run")
        self.assertFalse(self.remote.writes)

    def test_unchanged_default_floor_is_noop_and_explicit_rollback_is_reviewed(self):
        self.cfg["PROPOSED_MIN_VERSION"]="1.2.0"
        self.assertEqual(self.run_proposal()["status"],"no-op")
        self.assertFalse(self.remote.writes)
        self.cfg["PROPOSED_MIN_VERSION"]="1.1.0"
        self.assertEqual(self.run_proposal()["status"],"proposed")
        self.assertIn("1.2.0 → 1.1.0",self.remote.prs[0]["body"])

    def test_inputs_and_numeric_comparison(self):
        self.assertLess(minimum.version("0.9.9"), minimum.version("0.9.12"))
        self.assertEqual(minimum.version("v0.9.6-hotifx", minimum.TAG), (0,9,6))
        self.assertEqual(minimum.version("v1.3.0-rc.1+build.7", minimum.TAG), (1,3,0))
        for key,value in (("TARGET_ENV","../prod"),("PROPOSED_MIN_VERSION","01.3.0"),
                          ("PROPOSED_MIN_VERSION","1.3.0-rc.1"),("PROPOSED_MIN_VERSION","1.4.0"),
                          ("SOURCE_RELEASE_TAG","v1.3.0-01"),("SOURCE_RELEASE_TAG","x; echo bad"),
                          ("AUTOMATION_BOT","person"),("DRY_RUN","yes")):
            with self.subTest(key=key,value=value), patch.dict(os.environ,{key:value}):
                with self.assertRaises(minimum.ProposalError):minimum.settings()
        self.assertFalse(self.remote.calls)

    def test_all_environment_paths_are_fixed(self):
        for env in ("dev","staging","prod"):
            with self.subTest(env=env),patch.dict(os.environ,{"TARGET_ENV":env}):
                cfg=minimum.settings()
                self.assertEqual(cfg["path"],f"static/wallet/min-version/{env}/min-supported-version.json")

    def test_annotated_tags_are_resolved_to_terminal_commit(self):
        self.remote.tag_object={"type":"tag","sha":"b"*40}
        self.remote.tags={"b"*40:{"type":"tag","sha":"c"*40},"c"*40:{"type":"commit","sha":self.remote.source}}
        self.assertEqual(self.run_proposal()["source_sha"],self.remote.source)

    def test_tag_cycle_draft_and_partial_release_fail_before_writes(self):
        for field,value in (("draft",True),("body",None),("published_at",None),("id",True)):
            original=self.remote.release[field]
            with self.subTest(field=field):
                self.remote.release[field]=value
                with self.assertRaises(minimum.ProposalError):self.run_proposal()
                self.assertFalse(self.remote.writes)
            self.remote.release[field]=original
        self.remote.tag_object={"type":"tag","sha":"b"*40}
        self.remote.tags={"b"*40:self.remote.tag_object}
        with self.assertRaises(minimum.ProposalError):self.run_proposal()
        self.assertFalse(self.remote.writes)

    def test_every_preflight_read_failure_prevents_writes(self):
        for fragment in ("/releases/tags/","/git/ref/tags/","/git/ref/heads/","/contents/","/git/matching-refs/","/pulls?"):
            with self.subTest(fragment=fragment):
                self.remote.fail_read=fragment
                with self.assertRaises(minimum.ProposalError):self.run_proposal()
                self.assertFalse(self.remote.writes)

    def test_malformed_floor_schema_and_versions_stop_before_writes(self):
        tree=self.remote.commits[self.remote.base]["commit"]["tree"]["sha"]
        for value in ({}, {"min_supported_version":"1.2.0","extra":True},
                      {"min_supported_version":"1.2"}, {"min_supported_version":123}):
            with self.subTest(value=value):
                data=json.dumps(value).encode();self.remote.blobs[blob(data)]=data
                self.remote.trees[tree][self.cfg["path"]]=blob(data)
                with self.assertRaises(minimum.ProposalError):self.run_proposal()
                self.assertFalse(self.remote.writes)

    def test_foreign_branch_is_not_adopted_by_name(self):
        self.remote.refs[self.cfg["branch"]]=self.remote.base
        with self.assertRaises(minimum.ProposalError):self.run_proposal()
        self.assertFalse(self.remote.writes)

    def test_foreign_history_and_metadata_are_not_overwritten(self):
        self.run_proposal(); before=len(self.remote.writes)
        head=self.remote.refs[self.cfg["branch"]]
        original=copy.deepcopy(self.remote.commits[head])
        for mutation in (lambda c:c.update(author={"type":"User","login":"human"}),
                         lambda c:c["files"].append({"filename":"README.md","status":"modified"}),
                         lambda c:c.update(files=[]),
                         lambda c:c["commit"].update(verification={"verified":False,"reason":"unsigned"}),
                         lambda c:c["commit"].update(message="foreign edit")):
            self.remote.commits[head]=copy.deepcopy(original);mutation(self.remote.commits[head])
            with self.assertRaises(minimum.ProposalError):self.run_proposal()
            self.assertEqual(len(self.remote.writes),before)
        self.remote.commits[head]=original
        self.remote.prs[0]["body"]+="\nHuman edit"
        with self.assertRaises(minimum.ProposalError):self.run_proposal()
        self.assertEqual(len(self.remote.writes),before)

    def test_foreign_ancestor_and_other_app_pr_are_rejected(self):
        self.run_proposal();old=self.remote.refs[self.cfg["branch"]]
        self.next_request();self.run_proposal();before=len(self.remote.writes)
        self.remote.commits[old]["files"].append({"filename":"README.md","status":"modified"})
        with self.assertRaises(minimum.ProposalError):self.run_proposal()
        self.remote.commits[old]["files"].pop()
        self.remote.prs[0]["user"]={"type":"Bot","login":"another-app[bot]"}
        with self.assertRaises(minimum.ProposalError):self.run_proposal()
        self.assertEqual(len(self.remote.writes),before)

    def test_duplicate_prs_and_partial_pr_lists_fail_closed(self):
        self.run_proposal();before=len(self.remote.writes)
        duplicate=copy.deepcopy(self.remote.prs[0]);duplicate["number"]=2
        self.remote.prs.append(duplicate)
        with self.assertRaises(minimum.ProposalError):self.run_proposal()
        self.remote.prs.pop();self.remote.prs[0].pop("user")
        with self.assertRaises(minimum.ProposalError):self.run_proposal()
        self.assertEqual(len(self.remote.writes),before)

    def test_lost_ref_and_pr_creation_responses_reconcile_without_duplicates(self):
        for path in ("/git/refs","/pulls"):
            with self.subTest(path=path):
                self.remote=GitHub(self.cfg)
                minimum.api.side_effect=self.remote.api
                self.remote.lost=path
                self.run_proposal();self.run_proposal()
                self.assertEqual(len(self.remote.prs),1)
                self.assertEqual(sum(m=="POST" and p.endswith("/pulls") for m,p,_ in self.remote.writes),1)

    def test_partial_ref_without_pr_recovers_and_closed_request_stays_closed(self):
        self.remote.reject="/pulls"
        with self.assertRaises(minimum.ProposalError):self.run_proposal()
        self.assertIn(self.cfg["branch"],self.remote.refs)
        self.remote.reject=None;self.run_proposal()
        self.remote.prs[0]["state"]="closed"
        before=len(self.remote.writes)
        self.assertEqual(self.run_proposal()["status"],"no-op-closed")
        self.assertEqual(len(self.remote.writes),before)

    def test_partial_pr_update_recovers_original_new_commit(self):
        self.run_proposal();self.next_request();self.remote.reject="/pulls"
        with self.assertRaises(minimum.ProposalError):self.run_proposal()
        head=self.remote.refs[self.cfg["branch"]]
        self.remote.reject=None;self.run_proposal()
        self.assertEqual(self.remote.refs[self.cfg["branch"]],head)
        self.assertEqual(len(self.remote.prs),1)

    def test_lost_update_responses_and_immutable_object_failures(self):
        for operation in ("/git/blobs","/git/refs/heads/","/pulls/"):
            with self.subTest(operation=operation):
                self.remote=GitHub(self.cfg);minimum.api.side_effect=self.remote.api
                self.cfg["SOURCE_RELEASE_TAG"]="v1.3.0";self.cfg["PROPOSED_MIN_VERSION"]="1.3.0"
                self.remote.release.update(id=42,tag_name="v1.3.0")
                self.run_proposal();self.next_request();self.remote.lost=operation
                self.run_proposal();self.run_proposal()
                self.assertEqual(len(self.remote.prs),1)
        for operation in ("/git/trees","/git/commits"):
            with self.subTest(operation=operation):
                self.remote=GitHub(self.cfg);minimum.api.side_effect=self.remote.api
                self.remote.lost=operation
                with self.assertRaises(minimum.ProposalError):self.run_proposal()
                self.assertNotIn(self.cfg["branch"],self.remote.refs)
                self.assertFalse(self.remote.prs)
                self.run_proposal()
                self.assertEqual(len(self.remote.prs),1)

    def test_source_tag_move_is_not_adopted(self):
        self.run_proposal();before=len(self.remote.writes)
        self.remote.tag_object={"type":"commit","sha":"8"*40}
        with self.assertRaises(minimum.ProposalError):self.run_proposal()
        self.assertEqual(len(self.remote.writes),before)

    def test_metadata_edited_during_ref_update_is_preserved(self):
        self.run_proposal();self.next_request()
        self.remote.after_ref=lambda:self.remote.prs[0].update(body="Human edit")
        with self.assertRaises(minimum.ProposalError):self.run_proposal()
        self.assertEqual(self.remote.prs[0]["body"],"Human edit")


class TransportAndWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,ENV,clear=True))
        self.cfg=minimum.settings()

    def test_token_separation_and_no_default_branch_mutation(self):
        result=subprocess.CompletedProcess([],0,'{}','')
        with patch.object(subprocess,"run",return_value=result) as command:
            minimum.api(self.cfg,self.cfg["root"])
            env=command.call_args.kwargs['env']
            self.assertEqual(env['GH_TOKEN'],'fixture-read')
            self.assertNotIn('WRITE_TOKEN',env);self.assertNotIn('READ_TOKEN',env)
            minimum.api(self.cfg,self.cfg["root"]+'/git/blobs','POST',{'content':'x'})
            self.assertEqual(command.call_args.kwargs['env']['GH_TOKEN'],'fixture-write')
            for path,method,body in ((self.cfg['root']+'/git/refs/heads/master','PATCH',{'sha':'a'*40,'force':False}),
                    (self.cfg['root']+'/git/refs','POST',{'ref':'refs/heads/master','sha':'a'*40}),
                    (self.cfg['root']+'/pulls/1/merge','PUT',{}),
                    ('repos/ExampleOrg/wallet/git/refs','POST',{})):
                with self.subTest(path=path),self.assertRaises(minimum.ProposalError):minimum.api(self.cfg,path,method,body)
            self.cfg['dry_run']=True
            with self.assertRaises(minimum.ProposalError):minimum.api(self.cfg,self.cfg['root']+'/git/blobs','POST',{})

    def test_http_and_malformed_json_are_never_absence(self):
        for status in (403,404,429,500):
            with self.subTest(status=status),patch.object(subprocess,'run',return_value=subprocess.CompletedProcess([],1,'',f'gh: failed (HTTP {status})')):
                with self.assertRaises(minimum.ProposalError):minimum.api(self.cfg,self.cfg['root'])
        with patch.object(subprocess,'run',return_value=subprocess.CompletedProcess([],0,'not-json','')):
            with self.assertRaises(minimum.ProposalError):minimum.api(self.cfg,self.cfg['root'])

    def test_workflow_limits_operational_writes_to_manual_requests(self):
        text=(ROOT.parent/'workflows/propose-wallet-minimum.yml').read_text()
        tests=text.split('  test:\n',1)[1].split('\n  propose:',1)[0]
        operational=text.split('\n  propose:\n',1)[1]
        self.assertNotIn('secrets.',tests)
        self.assertNotIn('create-github-app-token',tests)
        self.assertIn("if: github.event_name == 'workflow_dispatch'",operational)
        self.assertIn('needs: test',operational)
        self.assertIn('default: true',text.split('      dry_run:',1)[1].split('\npermissions:',1)[0])
        for name in ('target_env','source_release_tag','proposed_min_version'):
            section=re.split(r'\n      (?=[a-z_]+:)',text.split('      '+name+':\n',1)[1],maxsplit=1)[0]
            self.assertIn('required: true',section)
        read=operational.split('      - name: Read release and proposal state',1)[1].split('      - name: Scope proposal writes',1)[0]
        write=operational.split('      - name: Scope proposal writes to static-assets',1)[1].split('      - name: Propose the selected minimum',1)[0]
        self.assertRegex(read,r'repositories: \|\s+wallet\s+static-assets')
        self.assertIn('permission-contents: read',read)
        self.assertIn('permission-pull-requests: read',read)
        self.assertIn('repositories: static-assets',write)
        self.assertNotIn('repositories: wallet',write)
        self.assertEqual(write.count("env.DRY_RUN == 'true' && 'read' || 'write'"),2)
        self.assertNotIn('git push',text)


if __name__ == '__main__':
    unittest.main()
