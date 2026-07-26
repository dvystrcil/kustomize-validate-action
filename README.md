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
reason) — always treated as empty. VPA coverage (D-012) is a planned
follow-up, not yet included — see homelab#529 AC2 for the open design
question (the cluster-wide audit's ArgoCD-destination cross-check isn't
derivable from one repo in isolation).

Only covers the kustomize-dir render pass, not the raw-manifest pass
below (repos without kustomize) — a Deployment and its InfisicalSecret
CR living in separate un-kustomized files isn't handled yet.

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
