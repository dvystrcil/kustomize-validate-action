#!/usr/bin/env python3
"""D-012 at PR time: a workload this PR adds or changes must have a VPA.

dvystrcil/homelab#529 AC2. The weekly cluster-wide audit
(homelab bin/audit-vpa-coverage.py) finds 63 uncovered workloads, so a PR
gate over EVERY workload would fail the next unrelated PR in most repos -- a
gate red on the day it lands, which gets disabled. This one judges only what
the PR touches:

  added    -- in head, not in base               -> needs a VPA
  changed  -- in both, spec differs              -> needs a VPA
              EXCEPT a pure image-tag change: a pin bump (Renovate) says
              nothing about sizing, and blocking it on inherited debt is the
              failure this design avoids
  untouched / removed                            -> not judged here

Anything that slips through (direct pushes, operator-generated workloads,
repos without this action) is the weekly audit's: each uncovered workload is
its own key on the rolling issue, so a NEW one notifies.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "vpa_coverage_check.py"
spec = importlib.util.spec_from_file_location("vcc", SCRIPT)
vcc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vcc)


def deploy(name="app", ns="apps", image="ghcr.io/x/app:1.0", cpu="50m", kind="Deployment"):
    return f"""apiVersion: apps/v1
kind: {kind}
metadata:
  name: {name}
  namespace: {ns}
spec:
  template:
    spec:
      containers:
      - name: c
        image: {image}
        resources:
          requests:
            cpu: {cpu}
---
"""


def vpa(target="app", ns="apps", kind="Deployment"):
    return f"""apiVersion: autoscaling.k8s.io/v1
kind: VerticalPodAutoscaler
metadata:
  name: {target}
  namespace: {ns}
spec:
  targetRef:
    apiVersion: apps/v1
    kind: {kind}
    name: {target}
  updatePolicy:
    updateMode: "Off"
---
"""


def check(head, base):
    return vcc.missing(vcc.parse(head), vcc.parse(base))


class TestJudgement(unittest.TestCase):
    def test_an_added_workload_without_a_vpa_fails(self):
        self.assertEqual(check(deploy(), ""), [("Deployment", "apps", "app", "added")])

    def test_an_added_workload_with_a_vpa_passes(self):
        self.assertEqual(check(deploy() + vpa(), ""), [])

    def test_an_added_statefulset_is_judged_too(self):
        self.assertEqual(check(deploy(kind="StatefulSet"), ""), [("StatefulSet", "apps", "app", "added")])

    def test_a_vpa_for_the_wrong_kind_does_not_count(self):
        self.assertEqual(check(deploy() + vpa(kind="StatefulSet"), ""),
                         [("Deployment", "apps", "app", "added")])

    def test_a_vpa_in_another_namespace_does_not_count(self):
        self.assertEqual(check(deploy() + vpa(ns="other"), ""),
                         [("Deployment", "apps", "app", "added")])

    def test_a_changed_workload_without_a_vpa_fails(self):
        self.assertEqual(check(deploy(cpu="100m"), deploy(cpu="50m")),
                         [("Deployment", "apps", "app", "changed")])

    def test_an_untouched_workload_without_a_vpa_is_not_judged(self):
        """Inherited debt belongs to the weekly audit, not this PR."""
        self.assertEqual(check(deploy(), deploy()), [])

    def test_an_image_only_change_is_not_judged(self):
        """A Renovate pin bump must not go red on a workload's old debt."""
        self.assertEqual(check(deploy(image="ghcr.io/x/app:2.0"), deploy(image="ghcr.io/x/app:1.0")), [])

    def test_an_image_change_plus_anything_else_is_judged(self):
        self.assertEqual(check(deploy(image="ghcr.io/x/app:2.0", cpu="99m"), deploy()),
                         [("Deployment", "apps", "app", "changed")])

    def test_a_removed_workload_is_not_judged(self):
        self.assertEqual(check("", deploy()), [])

    def test_other_kinds_are_ignored(self):
        cm = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  namespace: apps\n---\n"
        self.assertEqual(check(cm, ""), [])

    def test_no_namespace_on_both_still_matches(self):
        """A kustomization without `namespace:` renders neither with one."""
        head = deploy().replace("  namespace: apps\n", "") + vpa().replace("  namespace: apps\n", "")
        self.assertEqual(check(head, ""), [])


    def test_a_namespace_less_workload_matches_its_vpa_in_a_namespace(self):
        """obico#37: obico-web/-tasks set no namespace (Argo's destination
        supplies it) while base/vpa.yaml names `obico`. The render cannot know
        the destination, so an unset namespace matches; the gate called two
        covered Deployments uncovered and blocked a vpa-rightsize PR."""
        head = deploy().replace("  namespace: apps\n", "") + vpa()
        self.assertEqual(check(head, ""), [])

    def test_a_namespaced_workload_matches_a_namespace_less_vpa(self):
        head = deploy() + vpa().replace("  namespace: apps\n", "")
        self.assertEqual(check(head, ""), [])

    def test_an_unset_namespace_still_needs_the_same_kind_and_name(self):
        head = deploy().replace("  namespace: apps\n", "") + vpa(target="other")
        self.assertEqual(check(head, ""), [("Deployment", "", "app", "added")])

class TestCli(unittest.TestCase):
    def run_cli(self, head, base=None):
        d = Path(tempfile.mkdtemp())
        (d / "head.yaml").write_text(head)
        args = [sys.executable, str(SCRIPT), "--head", str(d / "head.yaml")]
        if base is not None:
            (d / "base.yaml").write_text(base)
            args += ["--base", str(d / "base.yaml")]
        return subprocess.run(args, capture_output=True, text=True)

    def test_a_gap_exits_1_and_names_the_workload(self):
        r = self.run_cli(deploy(), "")
        self.assertEqual(r.returncode, 1)
        self.assertIn("::error::", r.stdout)
        self.assertIn("Deployment apps/app", r.stdout)
        self.assertIn("added", r.stdout)
        self.assertIn("VPA-COVERAGE-PR-COMPLETE checked=1 missing=1", r.stdout)

    def test_clean_exits_0_with_the_marker(self):
        r = self.run_cli(deploy() + vpa(), "")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("VPA-COVERAGE-PR-COMPLETE checked=1 missing=0", r.stdout)

    def test_no_base_means_everything_is_added(self):
        """No base render (new kustomization dir) -- every workload is new."""
        self.assertEqual(self.run_cli(deploy()).returncode, 1)

    def test_unparseable_head_fails_closed(self):
        r = self.run_cli("kind: [unclosed")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
