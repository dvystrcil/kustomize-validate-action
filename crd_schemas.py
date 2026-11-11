#!/usr/bin/env python3
"""CustomResourceDefinitions -> kubeconform JSON schemas (#22).

    crd_schemas.py OUTDIR FILE...      # YAML/JSON streams; kubectl `List`s too

Writes OUTDIR/<group>/<kind>_<version>.json (kind lowercased) for every served
version, the same layout as the datree CRDs-catalog, so kubeconform reads it
with `-schema-location OUTDIR/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json`.
Non-CRD documents are ignored. Exit 0 unless OUTDIR cannot be written: a file
that does not parse is the render step's error to report, not this one's.

Why not the datree catalog (or kubeconform's openapi2jsonschema.py, which
builds it): the catalog is a third party's snapshot of SOME version of each
CRD (metallb's L2Advertisement from 2023-03-02 has no serviceSelectors,
metallb#22), and its converter adds `additionalProperties: false` under
`x-kubernetes-preserve-unknown-fields`, so it rejects what the apiserver
accepts (SealedSecret spec.template.metadata). This converter follows the
apiserver's structural-schema rules instead:

  object with properties          -> unknown fields rejected (as --validate=strict)
  x-kubernetes-preserve-unknown-fields -> left open
  nullable: true                  -> null allowed
  x-kubernetes-int-or-string      -> integer or string
  x-kubernetes-embedded-resource  -> apiVersion, kind, metadata allowed
  root metadata                   -> any object (ObjectMeta is the apiserver's,
                                     a CRD cannot constrain it beyond name)
  description text                -> dropped (size; kubeconform never reads it)

CEL rules (x-kubernetes-validations) are not translated: JSON Schema cannot
express them, and the apiserver still enforces them at apply time.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import yaml


def convert(node):
    """One openAPIV3Schema node -> JSON Schema, recursively. Never mutates."""
    if isinstance(node, list):
        return [convert(x) for x in node]
    if not isinstance(node, dict):
        return node
    # String `description`s are prose (most of a CRD's bytes); a property NAMED
    # description is a dict under `properties` and is kept.
    out = {k: convert(v) for k, v in node.items() if not (k == "description" and isinstance(v, str))}
    if out.pop("x-kubernetes-int-or-string", False):
        out.pop("type", None)
        out["anyOf"] = [{"type": "integer"}, {"type": "string"}]
    if out.pop("nullable", False) and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    if out.get("x-kubernetes-embedded-resource"):
        props = out.setdefault("properties", {})
        props.setdefault("apiVersion", {"type": "string"})
        props.setdefault("kind", {"type": "string"})
        props["metadata"] = {"type": "object"}
    if ("properties" in out and "additionalProperties" not in out
            and not out.get("x-kubernetes-preserve-unknown-fields")):
        out["additionalProperties"] = False
    return out


def convert_root(schema: dict) -> dict:
    s = copy.deepcopy(schema)
    props = s.setdefault("properties", {})
    props.setdefault("apiVersion", {"type": "string"})
    props.setdefault("kind", {"type": "string"})
    props["metadata"] = {"type": "object"}
    return convert(s)


def _crds(docs):
    for d in docs:
        if not isinstance(d, dict):
            continue
        if d.get("kind") == "List":
            yield from _crds(d.get("items") or [])
        elif d.get("kind") == "CustomResourceDefinition":
            yield d


def schemas_from_docs(docs) -> dict[str, dict]:
    """{"<group>/<kind>_<version>.json": schema} for every served version."""
    out = {}
    for c in _crds(docs):
        spec = c.get("spec") or {}
        group, kind = spec.get("group"), (spec.get("names") or {}).get("kind")
        if not group or not kind:
            continue
        for v in spec.get("versions") or []:
            schema = ((v.get("schema") or {}).get("openAPIV3Schema"))
            if not v.get("served", True) or not schema:
                continue
            out[f"{group}/{kind.lower()}_{v['name']}.json"] = convert_root(schema)
    return out


def write_schemas(schemas: dict[str, dict], outdir: Path) -> None:
    for rel, s in schemas.items():
        p = Path(outdir) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")


def _load(path: str) -> list:
    docs = []
    try:
        with open(path) as f:
            for d in yaml.safe_load_all(f):
                docs.append(d)
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        print(f"crd_schemas: {path}: not read ({type(e).__name__}); its CRDs, if any, are not used",
              file=sys.stderr)
    return docs


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.splitlines()[2].strip(), file=sys.stderr)
        return 2
    outdir, files = Path(argv[0]), argv[1:]
    schemas = {}
    for f in files:
        schemas.update(schemas_from_docs(_load(f)))
    write_schemas(schemas, outdir)
    print(f"crd_schemas: {len(schemas)} schema(s) from {len(files)} file(s) -> {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
