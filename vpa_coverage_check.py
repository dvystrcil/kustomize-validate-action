#!/usr/bin/env python3
"""D-012 at PR time: every Deployment/StatefulSet a PR ADDS or CHANGES needs a VPA.

dvystrcil/homelab#529 AC2. Compares this PR's rendered kustomize output
(--head) with the base commit's (--base) and fails for each workload that is

  added   -- in head, not in base, or
  changed -- in both with a different spec, other than a pure image change,

and has no VerticalPodAutoscaler in head whose targetRef names it (same
namespace, kind and name).

Deliberately NOT judged here:
  - untouched workloads: the weekly cluster-wide audit
    (homelab bin/audit-vpa-coverage.py) owns inherited debt. Each uncovered
    workload is its own key on its rolling issue, so a newly uncovered one --
    including anything that slipped past this check (direct push, a repo
    without this action, an operator-generated workload) -- posts a comment.
  - image-only changes: a Renovate pin bump says nothing about sizing, and
    failing it on a workload's old debt would block every bump in that repo.

Exemptions: homelab's policy/vpa-exemptions.yaml only exempts kube-system,
which no app repo renders, so there is none here. Adding one is a deliberate
change to this file.

    vpa_coverage_check.py --head head.yaml [--base base.yaml]
    exit 0 clean, 1 gaps (one ::error:: per workload), 2 unreadable input
"""
from __future__ import annotations

import argparse
import copy
import json
import sys

import yaml

KINDS = ("Deployment", "StatefulSet")
MARK = "VPA-COVERAGE-PR-COMPLETE"


def parse(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text or "") if isinstance(d, dict)]


def _key(d: dict) -> tuple[str, str, str]:
    md = d.get("metadata") or {}
    return (d.get("kind"), md.get("namespace") or "", md.get("name") or "")


def _workloads(docs: list[dict]) -> dict:
    return {_key(d): d for d in docs if d.get("kind") in KINDS}


def _without_images(spec: dict) -> dict:
    """The spec with every container/initContainer image blanked."""
    s = copy.deepcopy(spec or {})
    pod = ((s.get("template") or {}).get("spec") or {})
    for field in ("containers", "initContainers"):
        for c in pod.get(field) or []:
            if isinstance(c, dict):
                c.pop("image", None)
    return s


def _vpa_targets(docs: list[dict]) -> set[tuple[str, str, str]]:
    out = set()
    for d in docs:
        if d.get("kind") != "VerticalPodAutoscaler":
            continue
        ref = (d.get("spec") or {}).get("targetRef") or {}
        out.add((ref.get("kind"), (d.get("metadata") or {}).get("namespace") or "", ref.get("name")))
    return out


def missing(head: list[dict], base: list[dict]) -> list[tuple[str, str, str, str]]:
    """(kind, namespace, name, added|changed) for each touched workload without a VPA."""
    now, before, vpas = _workloads(head), _workloads(base), _vpa_targets(head)
    out = []
    for key in sorted(now):
        if key in before:
            a = json.dumps(_without_images(now[key].get("spec")), sort_keys=True)
            b = json.dumps(_without_images(before[key].get("spec")), sort_keys=True)
            if a == b:
                continue
            why = "changed"
        else:
            why = "added"
        if key not in vpas:
            out.append((*key, why))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--head", required=True)
    ap.add_argument("--base")
    a = ap.parse_args(argv)
    try:
        head = parse(open(a.head).read())
        base = parse(open(a.base).read()) if a.base else []
    except (OSError, yaml.YAMLError) as e:
        print(f"::error::vpa-coverage: cannot read rendered manifests ({e.__class__.__name__}: {e})")
        return 2
    gaps = missing(head, base)
    for kind, ns, name, why in gaps:
        where = f"{ns}/{name}" if ns else name
        print(f"::error::{kind} {where} ({why} in this PR) has no VerticalPodAutoscaler. "
              f"Add one targeting it (updateMode: \"Off\", D-012) in the same PR.")
    print(f"{MARK} checked={len(_workloads(head))} missing={len(gaps)}")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
