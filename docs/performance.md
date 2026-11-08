# Performance contract

Boundver's full `verify` and `status` gates intentionally recompute selected
facets from one captured Git source view. They do not trust a persistent cache.
Performance work therefore optimizes intrinsic invocation cost without
weakening snapshot identity, work budgets, or fail-closed behavior.

## Reproducible benchmark

Run the committed benchmark from the repository root:

```bash
python -I scripts/benchmark_runtime.py --runs 3
```

The script creates a temporary real Git repository containing 20 components
with eight implementation files each. Its contracts exercise six provider
families (`path-hash`, `json-canonical`, `openapi-canonical`,
`python-exports`, `typescript-exports`, and `leaf`), version extraction,
behavior envelopes, consumer edges, and slices. It measures both a clean HEAD
verification and full index verification after one small staged implementation
change.

Each result records:

- first-invocation and repeated wall/CPU time;
- Git process starts grouped by command;
- immutable Git blob reads and bytes;
- source-file reads and bytes;
- exact-tree hashes and provider extractions; and
- separate snapshot, config load, validation, lock load, fingerprint, and JSON
  rendering phases.

The source-work timings are inclusive, so nested rows are diagnostic
attribution rather than values to add together. The first run starts a fresh
Boundver operation and Git batch process; portable automation cannot flush the
host operating system's filesystem cache, so the report does not call this a
physical cold-disk measurement.

## Enforced CI target

### Larger adoption profile (local)

For v0.17 investigation, run:

```bash
python -I scripts/benchmark_adoption.py --runs 3 --components 58 --output adoption.json
```

This synthetic fixture adds file, tag, inherited and constant versions, ten
generated-artifact receipts with a shared source, consumer chains, and slices.
It measures validation, verification and generation separately, with first
and repeated runs, median/range/standard deviation, phase CPU/wall timings,
Git process counts and generation/verification source-read attribution.
Use `--components 116` to examine growth; the permitted range is 6 through 232.

The profile uses the in-process APIs. It does not include CLI startup or
atomic lock publication, and does not flush OS caches. It is not an adopter
benchmark or a new enforced CI latency promise. Validation uses the explicit
HEAD source; the CLI's compatibility default remains working-tree. Compare
equivalent source modes and record the host and fixture with any results.

The initial local Windows/Python 3.14 58-component run (three repetitions)
measured generation median 1.350 seconds (range 1.349-1.379), verification
1.405 seconds (1.386-1.437), and HEAD validation 0.446 seconds (0.445-0.452).
Generation started 38 Git processes; verification started 40. This fixture
does **not** reproduce the adopter's 90-315 second generation timings. More
representative file counts, tag/history topology, selectors and host/container
I/O need investigation before an optimization or numerical budget is justified.

### Existing verification contract

The Linux/Python 3.12 CI job runs:

```bash
python -I scripts/benchmark_runtime.py --runs 3 --enforce \
  --output runtime_benchmark.json
```

On the named `GitHub Actions ubuntu-latest, Python 3.12` contract, both the
20-component clean case and the small staged-change case must complete their
first invocation within 5 seconds and their repeated median within 3 seconds.
The enforced process totals are twelve for HEAD and twenty for index: six and
ten operational commands respectively, with each command preceded by an
uncached repository filter-configuration probe. The JSON report is retained
with the job artifacts.

Process ceilings are the tighter structural regression gate: fingerprinting
all 20 components must use one operation-scoped `git cat-file --batch`
transport, not one Git process per component or file. Repository filter
configuration is deliberately checked afresh before each Git command because
it can change during an operation and can name executable clean filters. The
wider wall ceilings remain stable on shared runners while still catching
severe extraction or I/O regressions.

## Optimization baseline

The 2026-08-31 pre-change profile on Windows 11 / Python 3.14 started 86 Git
processes for HEAD and 87 for the staged index. Fingerprinting alone started 81
processes and total wall time was approximately 2.02 seconds and 2.21 seconds,
respectively. The first operation-scoped blob-batching implementation cut those
totals to six and seven processes and measured approximately 0.21 seconds and
0.23 seconds on that host. The current hardened implementation uses three
isolated bounded blob readers and fresh per-command filter checks, producing
the twelve- and twenty-process CI ceilings above while remaining well below
the original 86/87-process baseline.

These local numbers explain the chosen optimization; they are not promises for
other hardware. The machine-readable CI contract above is the regression
authority. Persistent cross-process caching is deliberately outside this
contract because stale cache identity would introduce a separate correctness
and security boundary.
