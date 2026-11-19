<!--
Sync Impact Report — temporary review scratch, remove before committing
=======================================================================

Version change: (unversioned scaffold) → 1.0.0
Bump rationale: Initial adoption. The pre-existing file was an unfilled
  template scaffold (all placeholder tokens, no ratified principles, no
  version). This amendment introduces the first substantive constitution
  content: 4 core principles and 2 constraint sections.

Modified principles (scaffold placeholder → concrete):
  [PRINCIPLE_1_NAME] → I. Strict Type Safety
  [PRINCIPLE_2_NAME] → II. Modular Architecture
  [PRINCIPLE_3_NAME] → III. Unit Testing with pytest
  [PRINCIPLE_4_NAME] → IV. Zero Unapproved Third-Party Dependencies
  [PRINCIPLE_5_NAME] → (not used: the user scoped core principles to four)

Added sections:
  "Technology & Deployment Constraints" (was [SECTION_2_NAME])
  "Quality & Documentation Standards" (was [SECTION_3_NAME])

Removed sections: none

Dates:
  RATIFICATION_DATE = 2026-11-17 — inferred. The pre-existing file was an
  unfilled scaffold, so no earlier ratification existed; today is the
  first ratification of substantive content.
  LAST_AMENDED_DATE = 2026-11-17

Deferred TODOs: none.
  Note: the minimum supported Python version is intentionally left to the
  first feature plan rather than fixed in the constitution.
-->

# Botalert Constitution

## Core Principles

### I. Strict Type Safety

Every module, function, class, and public data structure in botalert MUST
carry complete type annotations (parameters and return values included).
A static type checker MUST be part of the approved dev toolchain (per
Principle IV) and MUST run on every change; zero type errors is the
condition for acceptance. Untyped escape hatches (`Any`, dynamic access)
MUST NOT be used without a documented justification recorded in the
change's plan.

Rationale: in a lightweight codebase the type surface is the primary
defense against defects; fully annotated, checked code stays safe to
refactor and remains human-readable.

### II. Modular Architecture

botalert MUST be organized as small, single-purpose modules, each
exposing a minimal, explicit public API. Inter-module dependencies MUST
be declared, acyclic, and limited to what the dependent actually needs; a
module MUST NOT reach into another module's internals. A new capability
MUST land in its own module before it is wired into existing ones.

Rationale: modularity keeps the codebase navigable and lightweight, makes
every unit independently testable (Principle III), and prevents the
coupling that typically drags in extra dependencies (Principle IV).

### III. Unit Testing with pytest

All non-trivial logic in botalert MUST have unit tests written with
pytest. Tests MUST be deterministic, isolated, and fast: the full unit
suite MUST complete in well under a minute. Any change that adds or
alters behavior MUST add or update the corresponding tests in the same
change, and the full suite MUST pass before the change is accepted.

Rationale: in a lightweight project the unit suite is the safety net that
scales; changing behavior and its tests together keeps regressions from
ever reaching a container build.

### IV. Zero Unapproved Third-Party Dependencies

The Python standard library MUST be the first choice for any capability.
No third-party package MAY be added without prior, explicit approval
recorded in the feature's spec or plan, together with the reason the
standard library or an already-approved dependency cannot cover it. The
approved dependency set MUST remain small and enumerable, and every
approved dependency MUST be pinned.

Rationale: each dependency expands supply-chain, maintenance, and image
size, which directly contradicts the simple-and-lightweight mandate;
pinning keeps container builds reproducible.

## Technology & Deployment Constraints

- **Language.** botalert MUST be implemented in Python, the choice that
  keeps the source human-readable. (The minimum supported Python version
  is pinned by the first feature plan, not here.)
- **Simplicity and lightness.** botalert MUST remain simple and
  lightweight. New complexity — extra modules, abstraction layers,
  configuration surface — MUST be justified in the change's plan, and when
  both a simple and a complex solution are viable, the simple solution
  MUST be chosen.
- **Container deployment.** botalert MUST be deployable as a container:
  the repository MUST include a `Dockerfile` that builds a reproducible
  image (pinned base image and dependencies), and the container MUST be
  the canonical way to run the project.

## Quality & Documentation Standards

- **Human-readable code.** Code MUST be plain, idiomatic Python written
  to be understood on first read: descriptive names, small functions, no
  unnecessary abstraction, and no cleverness that obscures intent.
- **Documentation.** Every public module, class, and function MUST carry
  a docstring stating its purpose and contract. The repository MUST
  include a README covering what botalert is, how to run it (locally and
  in a container), and how to extend it. Documentation MUST be updated in
  the same change that alters behavior or interface.

## Governance

This constitution supersedes any conflicting project practice, template,
or convention. A proposed change that conflicts with a principle MUST be
rejected or redesigned, or must be accompanied by a constitution
amendment — silent exceptions are not permitted.

- **Amendment procedure.** Amendments are made by editing this file with
  a proposed version bump, a short justification for each changed
  principle or section, and a Sync Impact Report as an HTML comment at
  the top of the file. The Sync Impact Report MUST be removed before the
  amended file is committed.
- **Versioning policy.** Versions use MAJOR.MINOR.PATCH: MAJOR for
  backward-incompatible principle removals or redefinitions; MINOR for
  new principles or sections, or materially expanded guidance; PATCH for
  clarifications, wording, and typo fixes.
- **Compliance review.** Every review MUST verify that the change
  complies with the core principles. Violations MUST be called out and
  resolved — fixed, justified in the plan, or addressed by amendment —
  before the change is accepted.

**Version**: 1.0.0 | **Ratified**: 2026-11-17 | **Last Amended**: 2026-11-17
