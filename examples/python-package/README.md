# Python package boundary

This example models `pkg/__init__.py` as the public export surface of a Python package. Its component version comes from `pkg/version.json`; discovery examples for standard manifests are covered in the getting-started guide.

The `python-exports` provider fingerprints the declared export file as an artifact. It does not import the package or infer semantic compatibility.

## Generate and verify

Install `boundver`, then run from the **boundver repository root**:

```bash
boundver generate \
  --config examples/python-package/boundary.config.json \
  --out examples/python-package/expected.boundary.lock.json \
  --source working-tree
boundver verify \
  --config examples/python-package/boundary.config.json \
  --lock examples/python-package/expected.boundary.lock.json \
  --source working-tree
```

Adding or removing a declared export changes the boundary fingerprint; a major version change also changes the compatibility facet.
