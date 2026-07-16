# kustomize-validate-action

Composite action: render every kustomization in the repo
(`kubectl kustomize`) and schema-validate the output with
[kubeconform](https://github.com/yannh/kubeconform), including CRD
schemas from the [datree CRDs catalog](https://github.com/datreeio/CRDs-catalog).

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
