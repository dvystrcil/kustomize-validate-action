"""Tests for crd_schemas.py: CRD openAPIV3Schema -> kubeconform JSON schema
(dvystrcil/kustomize-validate-action#22).

    python3 -m unittest tests.test_crd_schemas -v

The conversion must reject what the apiserver rejects and accept what it
accepts. Each rule below was checked against the live apiserver with
`kubectl apply --dry-run=server --validate=strict` on 2026-11-10:

- a misspelled field under a schema'd object -> `strict decoding error:
  unknown field "spec.typoTop"` (so: additionalProperties false)
- any field under an object marked x-kubernetes-preserve-unknown-fields
  (SealedSecret spec.template.metadata) -> accepted (so: left open). The
  datree catalog's converter ignores that marker, which is the false failure
  behind the action's old `-skip SealedSecret`.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("crd_schemas", ROOT / "crd_schemas.py")
cs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cs
_spec.loader.exec_module(cs)


def crd(kind="Widget", group="example.com", versions=None):
    versions = versions or [("v1", {"type": "object", "properties": {
        "spec": {"type": "object", "properties": {"size": {"type": "integer"}}}}})]
    return {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
            "metadata": {"name": f"{kind.lower()}s.{group}"},
            "spec": {"group": group, "names": {"kind": kind},
                     "versions": [{"name": v, "served": True, "storage": i == 0,
                                   "schema": {"openAPIV3Schema": s}} for i, (v, s) in enumerate(versions)]}}


class TestConvert(unittest.TestCase):
    def test_a_schemad_object_rejects_unknown_fields(self):
        out = cs.convert({"type": "object", "properties": {"a": {"type": "string"}}})
        self.assertIs(out["additionalProperties"], False)

    def test_preserve_unknown_fields_stays_open_even_with_properties(self):
        # SealedSecret spec.template.metadata, live CRD.
        out = cs.convert({"type": "object", "x-kubernetes-preserve-unknown-fields": True,
                          "properties": {"labels": {"type": "object"}}})
        self.assertNotIn("additionalProperties", out)

    def test_a_declared_map_keeps_its_value_schema(self):
        out = cs.convert({"type": "object", "additionalProperties": {"type": "string"}})
        self.assertEqual(out["additionalProperties"], {"type": "string"})

    def test_nullable_allows_null(self):
        out = cs.convert({"type": "object", "properties": {"m": {"type": "object", "nullable": True}}})
        self.assertEqual(out["properties"]["m"]["type"], ["object", "null"])

    def test_int_or_string(self):
        out = cs.convert({"x-kubernetes-int-or-string": True})
        self.assertEqual(out["anyOf"], [{"type": "integer"}, {"type": "string"}])

    def test_root_metadata_is_left_to_the_apiserver(self):
        # A CRD may not constrain ObjectMeta beyond name; kustomize's
        # creationTimestamp: null must never fail on it.
        out = cs.convert_root({"type": "object", "properties": {
            "metadata": {"type": "object", "properties": {"name": {"type": "string"}}}}})
        self.assertEqual(out["properties"]["metadata"], {"type": "object"})

    def test_root_accepts_apiversion_and_kind_when_the_crd_omits_them(self):
        out = cs.convert_root({"type": "object", "properties": {"spec": {"type": "object"}}})
        self.assertIn("apiVersion", out["properties"])
        self.assertIn("kind", out["properties"])

    def test_embedded_resource_accepts_type_and_object_meta(self):
        out = cs.convert({"type": "object", "x-kubernetes-embedded-resource": True,
                          "properties": {"spec": {"type": "object"}}})
        for k in ("apiVersion", "kind", "metadata"):
            self.assertIn(k, out["properties"])

    def test_description_text_is_dropped_but_a_field_named_description_is_kept(self):
        # Upstream prose is most of the bundle's size and kubeconform never
        # reads it; a PROPERTY called "description" is a dict, and must stay.
        out = cs.convert({"type": "object", "description": "prose", "properties": {
            "description": {"type": "string", "description": "more prose"}}})
        self.assertNotIn("description", {k for k, v in out.items() if isinstance(v, str)})
        self.assertEqual(out["properties"]["description"], {"type": "string"})

    def test_the_input_is_not_mutated(self):
        src = {"type": "object", "properties": {"a": {"type": "string"}}}
        cs.convert(src)
        self.assertNotIn("additionalProperties", src)


class TestFromManifests(unittest.TestCase):
    def test_one_file_per_served_version_at_the_datree_layout(self):
        d = crd(versions=[("v1", {"type": "object"}), ("v1beta1", {"type": "object"})])
        got = cs.schemas_from_docs([d, {"kind": "ConfigMap"}])
        self.assertEqual(sorted(got), ["example.com/widget_v1.json", "example.com/widget_v1beta1.json"])

    def test_unserved_versions_are_skipped(self):
        d = crd()
        d["spec"]["versions"][0]["served"] = False
        self.assertEqual(cs.schemas_from_docs([d]), {})

    def test_a_kubectl_list_is_read(self):
        got = cs.schemas_from_docs([{"kind": "List", "items": [crd()]}])
        self.assertEqual(list(got), ["example.com/widget_v1.json"])


class TestAgainstKubeconform(unittest.TestCase):
    """The converted schema through the kubeconform the action installs: what
    matters is what kubeconform does with it, not what the dict looks like."""

    L2_CRD = crd("L2Advertisement", "metallb.io", [("v1beta1", {"type": "object", "properties": {
        "apiVersion": {"type": "string"}, "kind": {"type": "string"}, "metadata": {"type": "object"},
        "spec": {"type": "object", "properties": {
            "ipAddressPools": {"type": "array", "items": {"type": "string"}},
            "serviceSelectors": {"type": "array", "items": {"type": "object", "properties": {
                "matchLabels": {"type": "object", "additionalProperties": {"type": "string"}}},
                "x-kubernetes-map-type": "atomic"}}}}}})])

    def setUp(self):
        if not shutil.which("kubeconform"):
            self.skipTest("kubeconform not installed")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        cs.write_schemas(cs.schemas_from_docs([self.L2_CRD]), self.tmp / "schemas")

    def conform(self, manifest: str) -> subprocess.CompletedProcess:
        f = self.tmp / "m.yaml"
        f.write_text(manifest)
        return subprocess.run(
            ["kubeconform", "-strict", "-summary",
             "-schema-location", str(self.tmp / "schemas") + "/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json",
             str(f)], capture_output=True, text=True)

    def test_metallb_22_service_selectors_validate(self):
        r = self.conform("apiVersion: metallb.io/v1beta1\nkind: L2Advertisement\nmetadata:\n  name: core-ingress\n"
                         "  creationTimestamp: null\nspec:\n  ipAddressPools: [first-pool]\n  serviceSelectors:\n"
                         "  - matchLabels:\n      gateway.networking.k8s.io/gateway-name: gateway\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Valid: 1", r.stdout)

    def test_a_misspelled_field_still_fails(self):
        r = self.conform("apiVersion: metallb.io/v1beta1\nkind: L2Advertisement\nmetadata:\n  name: x\n"
                         "spec:\n  serviceSelector: []\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("serviceSelector", r.stdout)


class TestCli(unittest.TestCase):
    def test_writes_files_and_reports_a_count(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "in.yaml").write_text("---\n" + json.dumps(crd()) + "\n---\nkind: ConfigMap\n")
        r = subprocess.run([sys.executable, str(ROOT / "crd_schemas.py"), str(tmp / "out"), str(tmp / "in.yaml")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((tmp / "out" / "example.com" / "widget_v1.json").is_file())
        self.assertIn("1 schema", r.stdout)

    def test_unparseable_input_is_not_fatal(self):
        # The render step already reports YAML errors; this pass must not add a
        # second, confusing failure (it only feeds schemas to kubeconform).
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "bad.yaml").write_text("kind: [unclosed\n")
        r = subprocess.run([sys.executable, str(ROOT / "crd_schemas.py"), str(tmp / "out"), str(tmp / "bad.yaml")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0 schema", r.stdout)


if __name__ == "__main__":
    unittest.main()
