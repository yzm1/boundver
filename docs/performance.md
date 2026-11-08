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

The profile uses the in-process APIs. It does not include CLI startup,
CLI-only advisory scans, or atomic lock publication, and does not flush OS caches. It is not an adopter
benchmark or a new enforced CI latency promise. Validation uses the explicit
HEAD source; the CLI's compatibility default remains working-tree. Compare
equivalent source modes and record the host and fixture with any results.

The initial local Windows/Python 3.14 58-component run (three repetitions)
measured generation median 1.350 seconds (range 1.349-1.379), verification
1.405 seconds (1.386-1.437), and HEAD validation 0.446 seconds (0.445-0.452).
Generation started 38 Git processes; verification started 40. This fixture
does **not** reproduce the adopter's 90-315 second generation timings. More
representative file counts, tag/history topology, selectors and host/container
I/O still need investigation. These API numbers alone cannot explain CLI
generation latency; the separate CLI profile below measures a previously
implicit advisory cost.

### HEAD generation CLI work budget (local)

```bash
python -I scripts/benchmark_generation_cli.py --runs 3 --include-inspect --enforce \
  --output generation-cli.json
```

This runs actual CLI dispatch in-process with `--dry-run`, using the same 58
components plus 512 tracked files outside every declared component. It includes
CLI preparation and rendering, but not interpreter startup, final file
publication or physical cold-disk measurements. The counterfactual mode
suppresses only the advisory warning helper, never validation or fingerprinting.
The explicit inspection mode runs that helper normally. Host, repeat variance,
Git commands, working-tree reads/bytes and computed lock hashes are recorded.

To investigate slow mounted storage, run the same synthetic workload separately
on each filesystem. `--fixture-parent` creates and removes a new temporary
repository under an existing scratch directory; never point it at an adopter's
checkout. For example:

```bash
python -I scripts/benchmark_generation_cli.py --outside-files 4000 \
  --outside-payload-bytes 24576 --fixture-parent /path/to/scratch \
  --storage-label mounted-scratch --runs 3 --include-inspect \
  --output mounted-profile.json
```

The storage label is supplied by the operator, not detected. Reports include
the outside-file payload size (each file also has a short identifying header).
Fixture creation permits up to 8,000 outside files and 64 KiB payload per file,
with a conservative 256 MiB aggregate outside-file budget. Defaults are unchanged.
This varies I/O volume and placement, not the provider/version/dependency shape;
it is not a reconstruction of any adopter's repository. Run comparisons
sequentially and record other workloads; first invocation does not mean a
flushed physical disk cache.

An exploratory Linux/Python 3.14 run with this larger outside-file workload
measured default HEAD generation at 0.42s on Linux-local storage and 9.96s on a
Windows-backed mount. Default generation read no working-tree content in either
case. Explicit inspection took 1.75s locally; on the mount its first/repeated
calls took 68.96s/319.89s. The mounted repeat read fewer files than the local
scan, and that report did not record advisory completion, so it is **not**
evidence of a completed 319-second scan. All modes produced identical lock
bytes. These were single-repeat, instrumented development runs with other local
work active, not controlled latency promises or proof of an adopter's cause.

New reports record `advisory_completed` and `advisory_failure_type`. The profile
contract refuses to count an unconfirmed inspection as a completed-scan timing,
even if generation exits successfully after an advisory warning. Old inspection
reports without completion metadata cannot satisfy that check.

Before the change, a local Windows/Python 3.14 run measured default generation
at a repeated median of 6.334s versus 3.492s quiet. The warning scan added 24 Git
processes and 1,337 working-tree reads, including 525 outside declared roots.
After making the scan explicit, a separate run measured default 2.530s, quiet
2.549s, and explicitly inspected 6.176s. Both runs computed the same canonical
lock bytes. Between-run quiet timings also moved, so the structural difference
and within-run comparisons are stronger evidence than a claimed absolute
speedup. This does not prove the cause of every adopter's reported latency.

The enforceable fixture contract is:

- Default HEAD generation performs zero working-tree content reads, zero
  advisory calls and zero `git write-tree` commands.
- Git starts are bounded by `20 + 2 * floor(component_count / 4)` (48 for 58
  components), allowing two independently guarded calls per tag prefix.
- Repeated default median is at most `1.5 * quiet median + 0.25 seconds` on the
  same fixture/host. This is not an arbitrary-repository latency promise.
- Every measured mode computes identical canonical lock bytes and succeeds.

This profile is local; no additional hosted job is scheduled. Small executable
regressions enforce the zero-read/index/advisory and lock-equivalence properties
in ordinary tests. The optional `--inspect-working-tree` diagnostic keeps its
existing no-fetch, filter, fsmonitor and resource protections; opting in may
still capture a contended index. The default optimization does not skip any
integrity check or add a persistent cache. User-facing timing/progress and
deadline-safe generation remain separate v0.17 work.

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

## Generation diagnostics (v0.17 development)

To see where generation spends time without changing its inputs:

```sh
boundver generate --source head --dry-run --progress --timing
```

`--progress` reports phase and component starts. `--timing` reports elapsed
phase and component times. Both write only to stderr, including with `--quiet`
or `--format json`; neither changes lock identities. Timing starts after CLI
argument parsing and repository discovery. Nested phase times overlap, so do
not add them together. Scoped generation still recomputes the complete lock
for integrity checks and reports those components too.

Diagnostics are limited to 256 progress lines, 32 distinct phase timings,
128 component timing rows and 64 KiB overall. The final summary records omitted
rows and lines. Labels are truncated and escaped for safe terminal display.
These options do not send data anywhere or impose an execution deadline by
themselves. Add `--timeout SECONDS` to bound computation before publication.
Atomic commit and final output are not hard wall-time bounded.

See the [publication and process-cleanup contract](generation-deadline-design.md)
for timeout behavior, cleanup allowances and native-provider limitations.

The summary distinguishes a dry run, a confirmed write, and publication not
attempted. If a write is interrupted or raises before its outcome can be
confirmed, it reports `attempted-outcome-unconfirmed`: inspect the destination
before retrying. Cancellation is not proof that the destination was unchanged.

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
