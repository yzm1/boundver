# Why SemVer fails at microservice and component boundaries

## The missing lockfile between Git and CI

SemVer has one job: encode a compatibility judgment in a version number. At
service boundaries, teams quietly give it a second job: determine whether a
change is compatible before anyone has established what changed. It cannot.

That distinction is easy to miss in a package registry. A maintainer changes a
library, decides whether its public API changed, assigns a version, and
publishes an artifact. Consumers see the version after the judgment has already
been made.

Inside a repository, the order is reversed. CI sees a commit before anyone has
classified its consequences. It has files, a Git graph, and perhaps a build
graph. It needs to decide which checks to run and which consumers might be at
risk. The version often has not changed yet. Sometimes there is no independently
versioned package at all.

This is where SemVer appears to fail at microservice and component boundaries.
The problem is not the versioning scheme. The problem is asking a version number
to detect a change that has not yet been understood.

## SemVer starts after the hard part

The [Semantic Versioning specification](https://semver.org/) begins with a
condition that deserves more attention: software using SemVer must declare a
public API. Major, minor, and patch numbers describe what happened to that API.
They do not discover where the API is, determine which repository files define
it, or identify who consumes it.

For a library, the package boundary often supplies useful structure. For a
service or platform component, the effective contract may be spread across:

- an OpenAPI document generated from infrastructure code;
- a JSON Schema maintained by another team;
- defaults in a YAML file;
- database migrations;
- a TypeScript declaration consumed by one application; and
- deployment behavior that is not represented in any language's type system.

The version field is downstream of all of those facts. Checking that it follows
SemVer cannot tell CI whether the facts changed.

Conventional Commits do not close the gap either. They record an author's
intention. An author can call a change a refactor while accidentally deleting a
schema property, or label a harmless documentation edit as a feature. The
commit message may be useful release input, but it is not independent evidence
about the repository state.

## Package lockfiles solved a similar state problem

A package manifest expresses acceptable possibilities. A lockfile records one
resolved reality.

For example, npm describes `package-lock.json` as the exact dependency tree
produced by resolution, committed so that developers and CI can reproduce the
same tree. The manifest still states policy; the lockfile captures the concrete
result. [npm's documentation](https://docs.npmjs.com/cli/v7/configuring-npm/package-lock-json/)
is explicit about that division of responsibility.

That pattern is useful beyond dependency installation:

| Dependency management | Contract management |
|---|---|
| The manifest declares acceptable dependencies. | A contract manifest declares components, boundary inputs, and consumers. |
| The resolver selects a concrete dependency graph. | A provider extracts a concrete contract identity from a Git snapshot. |
| The package lock records exact versions and integrity. | A contract lock records exact fingerprints and graph metadata. |
| CI rejects an install that disagrees with the lock. | CI rejects unreviewed contract drift from the lock. |

The analogy is not perfect. A dependency lock is prescriptive: install this
tree. A contract lock is observational: this is the reviewed identity of what
the repository publishes. But both convert a floating declaration into
version-controlled evidence.

Without that evidence, every pull request asks reviewers to reconstruct the old
contract from history, determine the new one, and remember every consumer. Most
teams eventually replace that work with broad test execution or path filters.
The first is expensive. The second quietly turns directory layout into an
architecture model.

## The useful unit is not “changed”

`git diff` answers a precise question: which recorded content differs between
two states? Git commits themselves identify complete source trees; the
[Git data model](https://git-scm.com/docs/user-manual) gives CI a strong,
immutable comparison substrate.

But “file changed” is too weak a result for a contract gate. Consider a service
with these edits in one pull request:

1. A private helper is renamed.
2. A retry default changes from three attempts to five.
3. A response property disappears from generated OpenAPI.
4. The service's compatibility family moves from version 2 to version 3.

All four are Git changes. They are not the same architectural event.

A useful contract model keeps separate identities for separate questions:

- **Exact:** did tracked component content or file identity change?
- **Behavior:** did a declared runtime-relevant input change?
- **Boundary:** did the artifact published to consumers change?
- **Compatibility:** did the declared compatibility family change?

These are signals, not verdicts. Boundary drift does not prove a breaking API
change. Behavior drift does not prove a production regression. They tell CI
which kind of evidence became stale and which downstream checks should run.

That is enough to improve the decision. A private rename can remain visible
without waking every consumer suite. A boundary change can trigger a
format-specific compatibility checker. A compatibility-family change can stop
promotion until consumers are coordinated.

## Why the lock must be Git-aware

Hashing a directory on disk sounds sufficient until CI has to answer which
directory it hashed.

A developer's working tree can contain ignored files, generated output, staged
changes, and unstaged changes. CI usually intends to verify an immutable commit.
A pre-commit check may intend to inspect the index. A local diagnosis may need
the working tree. Treating those as interchangeable creates a familiar failure:
the lock passes locally and fails after commit, or passes in CI while omitting a
new untracked boundary file.

A Git-aware contract lock binds its result to an explicit source state. It also
makes Git-known additions and deletions part of identity rather than relying on
a list of paths observed during the previous run. A new file must first be
staged or committed; an untracked file is deliberately outside every source
view. Selectors are evaluated against the chosen Git view, and the resulting
file identities are recorded deterministically.

Generated contracts require one more link. If `openapi.yaml` comes from an
infrastructure template, a fresh hash of a stale generated file is still stale.
The architecture therefore needs either a deterministic freshness check or a
receipt binding the generated boundary to its tracked inputs. A lockfile cannot
magically validate a generator it never runs.

## The consumer graph belongs beside the contract

Detecting drift without routing its consequences leaves the most expensive
question unanswered: who has to care?

Build graphs can answer this when every producer and consumer belongs to one
authoritative build system. Microservice repositories often do not have that
luxury. A Python service, a mobile client, an infrastructure stack, and a
TypeScript application may have different task graphs—or none that cross the
team boundary.

A small declared consumer graph is less ambitious than automatic dependency
discovery, but more honest. It says: these are the relationships the repository
claims. When a boundary moves, CI can emit direct consumers or walk the
transitive closure. When the declaration is wrong, review the declaration as an
architecture defect.

This does not replace Bazel, Nx, Pants, or another build graph. It creates a
portable contract layer that can hand those systems a narrower, better-labeled
event.

## What a contract lockfile cannot prove

Content identity is deliberately weaker than semantic compatibility.

An OpenAPI-aware tool can determine whether a particular schema edit violates
its compatibility rules. A Protobuf checker understands field-number hazards.
A compiler understands a language's type relationships. A consumer test can
exercise behavior that no schema captures.

A general contract lockfile should not imitate those tools with shallow parsing
and stronger claims. Its job is to establish three facts reliably:

1. the declared contract identity moved;
2. the movement occurred in a specific Git state; and
3. the declared consumers are the next verification targets.

The semantic checker still judges the changed artifact. The build system still
schedules work. The consumer suite still tests integration. The lockfile makes
sure those tools are invoked because of repository evidence rather than a path
filter or a hopeful commit label.

The limits are equally important:

- An omitted file remains invisible to the facet that omitted it.
- An undeclared consumer remains absent from impact output.
- Updating a lock without reviewing the diff can rubber-stamp a real change.
- A stable boundary artifact cannot reveal undocumented runtime behavior.
- A changed fingerprint says “re-evaluate,” not “breaking.”

These are not reasons to avoid the model. They are reasons to keep its claims
narrow and its declarations reviewable.

## The architecture in one flow

The mechanism can be small:

```text
declared components + selectors + consumers
                    │
                    ▼
          explicit Git source state
                    │
                    ▼
       bounded, deterministic extraction
                    │
                    ▼
       committed contract lockfile
                    │
          compare on the next change
                    ▼
       drift facets + affected consumers
                    │
                    ▼
 semantic checks, builds, and consumer tests
```

The value comes from the boundaries between those steps. Configuration states
what matters. Git states which bytes are under review. Extraction states how an
artifact becomes an identity. The lock states what was accepted. The consumer
graph states where new evidence is required.

## This is where Boundver fits

[Boundver](https://github.com/yzm1/boundver) is one implementation of this
contract-lockfile model. It records exact, behavior, boundary, and compatibility
fingerprints for declared components, compares explicit Git source modes, and
reports direct or transitive consumers when boundary or compatibility
identities drift.

It intentionally does not decide whether a changed OpenAPI document is backward
compatible, discover the dependency graph, or run consumer tests. It is the
repository-level signal between Git and those systems.

The larger idea does not depend on one tool: architecture declarations need a
resolved, reviewable state just as dependency declarations do. SemVer can then
do the job it was designed for—communicating the compatibility decision—rather
than being asked to discover the evidence for that decision after the fact.

That is the missing lockfile at service boundaries.
