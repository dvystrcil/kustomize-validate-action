# kustomize-validate-action

Composite action: render every kustomization in the repo
(`kubectl kustomize`), schema-validate the output with
[kubeconform](https://github.com/yannh/kubeconform) (including CRD
schemas from the [datree CRDs catalog](https://github.com/datreeio/CRDs-catalog)),
and check homelab's own D-013 coverage rule — every workload consuming
an InfisicalSecret-managed Secret must carry `reloader.stakater.com/auto`
on itself ([homelab#529](https://github.com/dvystrcil/homelab/issues/529)).

Born from an incident: a malformed `resources:` list (two entries folded
into one scalar by an indent-blind append) merged clean and **halted
ArgoCD reconciliation** for the app until repaired. `kustomize build`
on the PR would have gone red in seconds.

## Usage

```yaml
name: validate
on: [pull_request]
jobs:
  kustomize:
    runs-on: <repo>-runner        # or ubuntu-latest off-cluster
    steps:
      - uses: actions/checkout@v5
      - uses: dvystrcil/kustomize-validate-action@v1
```

Inputs: `paths` (space-separated kustomization dirs; default =
auto-discover, skipping `base/` since overlays reach them),
`kubeconform_flags` (e.g. `-skip SomeKind`).

## D-013 Reloader coverage check

Runs `reloader_coverage_check.py` (vendored in this repo — see its own
header) against each kustomization dir's own rendered output
(`--from-manifest`, no cluster access) — a Deployment/StatefulSet/CronJob
and the `InfisicalSecret` CR managing the Secret it consumes are expected
to live in the same repo, so this is fully self-contained per PR.

**Vendored, not fetched.** The checker's canonical source is
`dvystrcil/homelab`'s `bin/audit-reloader-coverage.py`
([homelab#529](https://github.com/dvystrcil/homelab/issues/529)) — but
that repo is private, and `raw.githubusercontent.com` 404s unauthenticated
requests to private-repo content (discovered live building this; wiring
GitHub App auth into every consuming repo's one-line `uses:` call was
judged not worth it for one small, stable script). **When homelab's
source changes, `reloader_coverage_check.py` here needs a manual sync.**

Exemptions aren't supported in this composite action (same private-repo
reason) — always treated as empty.

Only covers the kustomize-dir render pass, not the raw-manifest pass
below (repos without kustomize) — a Deployment and its InfisicalSecret
CR living in separate un-kustomized files isn't handled yet.

## D-012 VPA coverage check (pull requests only)

`vpa_coverage_check.py` (homelab#529 AC2). On a pull request the action also
renders the PR's **base** commit and fails for every Deployment/StatefulSet the
PR **adds or changes** that has no `VerticalPodAutoscaler` in the rendered
output targeting it (same namespace, kind and name):

| workload | judged? |
|---|---|
| added in this PR | yes |
| changed in this PR (any spec change) | yes |
| changed **only** in its container images (e.g. a Renovate pin bump) | no: a pin says nothing about sizing, and failing it on old debt would block every bump |
| untouched, or removed | no |

Untouched workloads aren't judged here **on purpose**. The weekly cluster-wide
audit (homelab `bin/audit-vpa-coverage.py`) owns inherited debt and is the
backstop for anything that slips past this check: direct pushes, repos without
this action, operator-generated workloads. Each uncovered workload is its own
key on that audit's rolling issue, so a *newly* uncovered one posts a comment.

The ArgoCD-destination cross-check the cluster audit does is dropped here: a
repo running this action is an ArgoCD app repo by construction. There are no
exemptions (homelab's `policy/vpa-exemptions.yaml` only exempts `kube-system`,
which no app repo renders). If the PR base can't be checked out, the check
fails closed; on `push` events it's skipped.

## What it deliberately does NOT do

**No `kubectl apply --dry-run=server`.** Server-side dry-run authorizes
with the same write verbs as a real apply, and CI runners hold a
read-only cluster grant on purpose. Full-schema + admission validation
happens laptop-side in the tooling that generates manifests
(e.g. homelab `bin/vpa-rollout.py`'s pre-push gate). The two tiers here
(structural render + offline schema with CRD catalog) catch the classes
that have actually bitten: malformed lists and silently-dropped fields.

## License

Code is [MIT](LICENSE).
