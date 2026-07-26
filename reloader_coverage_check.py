#!/usr/bin/env python3
"""VENDORED COPY -- homelab#529 (kustomize-validate-action#6).

This file is copied from `dvystrcil/homelab`'s `bin/audit-reloader-coverage.py`
at commit 9d888d980c09bf214b2b5f2ca371074611c0a152. It is NOT fetched at
CI time: `dvystrcil/homelab` is a private repo, and raw.githubusercontent.com
returns 404 for unauthenticated requests to private-repo content --
discovered live while building this, after a pinned-SHA fetch design
failed in local testing. Wiring cross-repo GitHub App auth into every
consuming repo's one-line `uses: dvystrcil/kustomize-validate-action@v1`
call was judged not worth the added complexity for one small, stable,
already-tested script -- vendoring + manual sync is the simpler
equivalent of the same "pinned, not floating" tradeoff.

**When homelab's `bin/audit-reloader-coverage.py` changes, this file
needs a matching manual update.** Only the two `gather_*_from_docs()`
functions and the CLI's `--from-manifest`/`--exemptions-file` flags are
actually used here; the cluster-querying gatherers and `--upsert-issue`
path are dead code in this context but kept for drift-diffing ease
(a `diff` against the homelab source stays meaningful).

Exemptions are NOT supported in this composite action (always empty) --
a per-PR check has no clean way to see homelab's cluster-wide policy
file without the same auth problem above. Known limitation, not silently
dropped: if a real repo needs an exemption from this specific check,
that's a signal to revisit this design, not to work around it quietly.

---

Reloader annotation coverage audit (homelab#289): every Deployment/
StatefulSet/CronJob that consumes an InfisicalSecret-managed Secret (via
envFrom, env.valueFrom.secretKeyRef, a volume, or imagePullSecrets) should
carry `reloader.stakater.com/auto: "true"` on its own top-level metadata,
so a secret rotation actually rolls the consuming pod instead of
requiring a manual restart — or an explicit exemption in
policy/reloader-exemptions.yaml. Workloads that don't reference any
Infisical-managed secret are out of scope entirely (not a gap, not
"covered", just irrelevant).

The annotation is a Reloader property of the CONSUMING workload's own
top-level `metadata.annotations` (Deployment/StatefulSet/CronJob), not
the pod template underneath it and not the Secret. Verified against live
examples already correctly wired in this cluster (n8n, oikb, mcp-server,
sd-webui-rcom — all at `metadata.annotations`, none under
`spec.template.metadata`). This corrects homelab#289's original proposed
shape, which would have put the annotation on the InfisicalSecret's
managed-secret template instead.

Report-only. Weekly CI run (reloader-coverage-audit.yaml) upserts ONE
rolling issue, updated in place — never one-per-week. Mirrors
bin/audit-vpa-coverage.py's shape (homelab#456).

    bin/audit-reloader-coverage.py                 # print report, exit 0
    bin/audit-reloader-coverage.py --strict        # exit 1 if any gap
    bin/audit-reloader-coverage.py --upsert-issue  # also upsert the rolling issue

Context: homelab#289, architecture/upgrade-playbook.md (secret rotation
section).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EXEMPTIONS_FILE = REPO_ROOT / "policy" / "reloader-exemptions.yaml"
ROLLING_ISSUE_TITLE = "Reloader coverage audit report (rolling)"
ROLLING_ISSUE_REPO = "dvystrcil/homelab"
RELOADER_ANNOTATION = "reloader.stakater.com/auto"

WORKLOAD_KINDS = (("Deployment", "deployments"),
                  ("StatefulSet", "statefulsets"),
                  ("CronJob", "cronjobs"))


@dataclass
class Workload:
    namespace: str
    kind: str                       # Deployment | StatefulSet | CronJob
    name: str
    has_annotation: bool = False
    secrets: tuple[str, ...] = field(default_factory=tuple)
    in_scope: bool = False          # references >=1 Infisical-managed secret

    @property
    def subject(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass
class Gap:
    kind: str        # workload-missing-reloader-annotation | stale-exemption
    subject: str
    detail: str = ""


@dataclass
class Exempt:
    subject: str
    detail: str = ""


# ------------------------------------------------------------ evaluation core

def evaluate_reloader_coverage(workloads: list[Workload], exemptions: dict
                               ) -> tuple[list[Workload], list[Exempt], list[Gap]]:
    ns_exempt = {e["namespace"]: e.get("reason", "")
                 for e in exemptions.get("workloads", []) if "name" not in e}
    wl_exempt = {(e["namespace"], e["name"]): e.get("reason", "")
                 for e in exemptions.get("workloads", []) if "name" in e}
    covered, exempt, gaps = [], [], []
    for wl in workloads:
        if not wl.in_scope:
            continue
        if wl.has_annotation:
            covered.append(wl)
        elif wl.namespace in ns_exempt:
            exempt.append(Exempt(wl.subject, ns_exempt[wl.namespace]))
        elif (wl.namespace, wl.name) in wl_exempt:
            exempt.append(Exempt(wl.subject, wl_exempt[(wl.namespace, wl.name)]))
        else:
            gaps.append(Gap(
                "workload-missing-reloader-annotation", wl.subject,
                f"{wl.kind} consumes Infisical-managed secret(s) "
                f"{', '.join(wl.secrets)} but lacks {RELOADER_ANNOTATION}"))
    return covered, exempt, gaps


def missing_namespaces(gaps: list[Gap]) -> list[str]:
    """Distinct, sorted namespaces with at least one
    workload-missing-reloader-annotation gap — the machine-readable
    interface bin/reloader-rollout.py consumes instead of scraping the
    markdown report."""
    return sorted({g.subject.split("/", 1)[0]
                  for g in gaps if g.kind == "workload-missing-reloader-annotation"})


def find_stale_exemptions(exemptions: dict, all_workloads: list[Workload]
                          ) -> list[Gap]:
    live_wl = {(w.namespace, w.name) for w in all_workloads}
    live_ns = {w.namespace for w in all_workloads}
    gaps = []
    for e in exemptions.get("workloads", []):
        if "name" in e and (e["namespace"], e["name"]) not in live_wl:
            gaps.append(Gap("stale-exemption", f'{e["namespace"]}/{e["name"]}',
                            "exempted workload no longer exists"))
        elif "name" not in e and e["namespace"] not in live_ns:
            gaps.append(Gap("stale-exemption", e["namespace"],
                            "exempted namespace has no in-scope workloads"))
    return gaps


def render_report(covered: list[Workload], exempt: list[Exempt],
                  gaps: list[Gap], stale: list[Gap], generated_at: str) -> str:
    total = len(covered) + len(exempt) + len(gaps)
    lines = [
        f"# Reloader coverage audit — {generated_at}",
        "",
        f"Rule: homelab#289 · every workload consuming an "
        f"InfisicalSecret-managed Secret should carry "
        f"`{RELOADER_ANNOTATION}: \"true\"` on itself (not the Secret)",
        "",
        f"**Coverage: {len(covered)}/{total} covered, "
        f"{len(exempt)} exempt, {len(gaps)} missing** · "
        f"{len(stale)} stale exemptions",
        "",
    ]
    if gaps:
        lines += ["## Workloads missing the annotation", ""]
        lines += [f"- `{g.subject}` — {g.detail}" for g in gaps] + [""]
    if stale:
        lines += ["## Stale exemptions (clean these up)", ""]
        lines += [f"- `{g.subject}` — {g.detail}" for g in stale] + [""]
    if exempt:
        lines += ["## Exempt", ""]
        lines += [f"- `{e.subject}` — {e.detail}" for e in exempt] + [""]
    lines += [f"`AUDIT-COMPLETE missing={len(gaps)} stale={len(stale)}`"]
    return "\n".join(lines)


# ---------------------------------------------------------------- gatherers

def _run(cmd: list[str], timeout: int = 90) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            print(f"[{cmd[0]} exit {r.returncode}] {r.stderr.strip()[:500]}",
                  file=sys.stderr)
            return None
        return r.stdout
    except Exception as exc:
        print(f"[{cmd[0]} raised] {exc}", file=sys.stderr)
        return None


def _kubectl_json(args: list[str]):
    out = _run(["kubectl", *args, "-o", "json"])
    return json.loads(out) if out else None


def gather_managed_secrets() -> set[tuple[str, str]]:
    """(namespace, secretName) for every Secret an InfisicalSecret CR manages."""
    data = _kubectl_json(["get", "infisicalsecret", "-A"])
    if data is None:
        sys.exit("FATAL: could not list InfisicalSecret CRs")
    out = set()
    for i in data["items"]:
        ns = i["metadata"]["namespace"]
        spec = i["spec"]
        if "managedSecretReference" in spec:
            out.add((ns, spec["managedSecretReference"]["secretName"]))
        elif "managedKubeSecretReferences" in spec:
            for r in spec["managedKubeSecretReferences"]:
                out.add((ns, r["secretName"]))
    return out


def gather_managed_secrets_from_docs(docs: list[dict | None]) -> set[tuple[str, str]]:
    """Same rule as gather_managed_secrets(), applied to already-rendered
    YAML documents (e.g. `kubectl kustomize` output) instead of a live
    `kubectl get` -- homelab#529, the per-repo/per-PR variant of this
    check. `docs` may contain None entries (yaml.safe_load_all yields one
    for each stray `---` separator); those are skipped, not an error."""
    out = set()
    for doc in docs:
        if not doc or doc.get("kind") != "InfisicalSecret":
            continue
        ns = doc["metadata"]["namespace"]
        spec = doc["spec"]
        if "managedSecretReference" in spec:
            out.add((ns, spec["managedSecretReference"]["secretName"]))
        elif "managedKubeSecretReferences" in spec:
            for r in spec["managedKubeSecretReferences"]:
                out.add((ns, r["secretName"]))
    return out


def gather_workloads_from_docs(docs: list[dict | None]) -> list[Workload]:
    """Manifest-based counterpart to gather_workloads() -- homelab#529.
    Both the managed-secret set and the workloads come from the SAME
    rendered document list (one repo's own kustomize output), so a
    Deployment and the InfisicalSecret CR that manages its Secret are
    expected to live in the same PR. Reuses pod_spec_of/
    object_annotations_of/referenced_managed_secrets unchanged -- only
    the data source (rendered docs vs. live kubectl) differs from
    gather_workloads()."""
    managed = gather_managed_secrets_from_docs(docs)
    kind_map = dict(WORKLOAD_KINDS)
    wls = []
    for doc in docs:
        if not doc or doc.get("kind") not in kind_map:
            continue
        kind = doc["kind"]
        ns = doc["metadata"]["namespace"]
        name = doc["metadata"]["name"]
        spec = pod_spec_of(kind, doc)
        secrets = referenced_managed_secrets(spec, ns, managed)
        annotations = object_annotations_of(doc)
        has_annotation = annotations.get(RELOADER_ANNOTATION) == "true"
        wls.append(Workload(ns, kind, name, has_annotation,
                            tuple(secrets), in_scope=bool(secrets)))
    return wls


def load_docs_from_manifest(path: Path) -> list[dict | None]:
    return list(yaml.safe_load_all(path.read_text()))


def pod_spec_of(kind: str, item: dict) -> dict:
    if kind == "CronJob":
        return item["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return item["spec"]["template"]["spec"]


def object_annotations_of(item: dict) -> dict:
    """Reloader watches the annotation on the workload CONTROLLER object's
    own top-level metadata (Deployment/StatefulSet/CronJob), not its pod
    template -- verified against live examples in this cluster (n8n,
    oikb, mcp-server, sd-webui-rcom all carry it at metadata.annotations;
    none have it under spec.template.metadata). Same for all three kinds
    -- no per-kind nesting needed."""
    return item.get("metadata", {}).get("annotations", {})


def referenced_managed_secrets(pod_spec: dict, namespace: str,
                               managed: set[tuple[str, str]]) -> list[str]:
    found = set()
    for c in pod_spec.get("containers", []) + pod_spec.get("initContainers", []):
        for ef in c.get("envFrom", []):
            n = ef.get("secretRef", {}).get("name")
            if n and (namespace, n) in managed:
                found.add(n)
        for e in c.get("env", []):
            n = e.get("valueFrom", {}).get("secretKeyRef", {}).get("name")
            if n and (namespace, n) in managed:
                found.add(n)
    for v in pod_spec.get("volumes", []):
        n = v.get("secret", {}).get("secretName")
        if n and (namespace, n) in managed:
            found.add(n)
    for ips in pod_spec.get("imagePullSecrets", []):
        n = ips.get("name")
        if n and (namespace, n) in managed:
            found.add(n)
    return sorted(found)


def gather_workloads() -> list[Workload]:
    managed = gather_managed_secrets()
    wls = []
    for kind, res in WORKLOAD_KINDS:
        data = _kubectl_json(["get", res, "-A"])
        if data is None:
            sys.exit(f"FATAL: could not list {res}")
        for item in data["items"]:
            ns = item["metadata"]["namespace"]
            name = item["metadata"]["name"]
            spec = pod_spec_of(kind, item)
            secrets = referenced_managed_secrets(spec, ns, managed)
            annotations = object_annotations_of(item)
            has_annotation = annotations.get(RELOADER_ANNOTATION) == "true"
            wls.append(Workload(ns, kind, name, has_annotation,
                                tuple(secrets), in_scope=bool(secrets)))
    return wls


def load_exemptions(path: Path | None = None) -> dict:
    """Default (no path): the cluster-wide policy file, required to
    exist (this is the weekly whole-cluster audit's normal path). When
    called from --from-manifest mode with an explicit path that doesn't
    exist, exemptions are simply empty rather than fatal -- a per-repo
    PR check has no obligation to carry a copy of homelab's policy file
    unless the caller (e.g. kustomize-validate-action) fetches one."""
    target = path or EXEMPTIONS_FILE
    if not target.exists():
        if path is not None:
            return {}
        sys.exit(f"FATAL: {EXEMPTIONS_FILE} missing")
    return yaml.safe_load(target.read_text()) or {}


# ------------------------------------------------------------- issue upsert

def upsert_rolling_issue(report: str) -> None:
    out = _run(["gh", "issue", "list", "--repo", ROLLING_ISSUE_REPO,
               "--search", f'"{ROLLING_ISSUE_TITLE}" in:title',
               "--state", "open", "--json", "number,title"])
    if out is None:
        sys.exit("FATAL: gh issue list failed")
    issues = json.loads(out)
    num = next((i["number"] for i in issues
               if i["title"] == ROLLING_ISSUE_TITLE), None)
    if num:
        if _run(["gh", "issue", "edit", str(num),
                "--repo", ROLLING_ISSUE_REPO, "--body", report]) is None:
            sys.exit(f"FATAL: gh issue edit #{num} failed")
        print(f"updated rolling issue #{num}")
    else:
        if _run(["gh", "issue", "create", "--repo", ROLLING_ISSUE_REPO,
                "--title", ROLLING_ISSUE_TITLE, "--body", report]) is None:
            sys.exit("FATAL: gh issue create failed")
        print("created rolling issue")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 if any gap or stale exemption")
    ap.add_argument("--upsert-issue", action="store_true")
    ap.add_argument("--missing-namespaces", action="store_true",
                    help="print distinct namespaces with a gap, one per "
                         "line, instead of the markdown report — for "
                         "driving bin/reloader-rollout.py per namespace")
    ap.add_argument("--from-manifest", type=Path, default=None,
                    help="homelab#529: check ONE already-rendered "
                         "multi-doc YAML file (e.g. `kubectl kustomize` "
                         "output) instead of querying the live cluster — "
                         "the per-repo/per-PR mode. No kubectl needed. "
                         "Stale-exemption checking is skipped (not "
                         "meaningful for a single repo's own manifests).")
    ap.add_argument("--exemptions-file", type=Path, default=None,
                    help="override the exemptions file path (default: "
                         "policy/reloader-exemptions.yaml relative to "
                         "this repo). With --from-manifest and no "
                         "override, or a path that doesn't exist, "
                         "exemptions are simply empty rather than fatal.")
    args = ap.parse_args()

    exemptions = load_exemptions(args.exemptions_file)

    if args.from_manifest:
        docs = load_docs_from_manifest(args.from_manifest)
        workloads = gather_workloads_from_docs(docs)
        stale: list[Gap] = []
    else:
        workloads = gather_workloads()
        stale = find_stale_exemptions(exemptions, workloads)

    covered, exempt, gaps = evaluate_reloader_coverage(workloads, exemptions)

    if args.missing_namespaces:
        for ns in missing_namespaces(gaps):
            print(ns)
        return 0

    report = render_report(covered, exempt, gaps, stale,
                           datetime.now(timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ"))
    print(report)
    if args.upsert_issue:
        upsert_rolling_issue(report)
    if args.strict and (gaps or stale):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
