# Decision and change lifecycle

This guide covers grouped decision adoption and narrow, specification-preserving display repairs. RPC examples use `daikibo call METHOD --json JSON`; IDs and adapter names are placeholders.

## Bind each answer choice to its effect

For a proposal with a linked change, conflict, policy update or superseded decision, the built-in choices have fixed meanings: `approve` accepts the proposal's linked effects, `keep_existing` retains the current requirements and declines its linked change, `reject` closes the proposal without applying those effects, and `defer` leaves it awaiting a later answer. `keep_existing` closes the linked change's notice and scoped change blocks, withdraws that unapplied change and closes its unselected sibling decisions. It preserves the accepted artifacts and any supersession ancestors. The choice and its effect appear in the review material, so the consistency reviewer sees the action that will be taken.

Custom choices on a proposal with linked effects must declare their meaning in `choice_effects`; `accept` applies the linked effects and `keep_existing` retains the existing specification. The reserved choices cannot be remapped. A side-effecting proposal cannot use a custom `record_only` choice. For a proposal with no linked effects, an undeclared custom option remains a record-only audit choice for compatibility. A choice label by itself never authorizes linked effects. Batch packets retain all linked-change material for review, while their final projection and apply step use the same selected effect; archive validation checks the resulting changed-artifact set.

For example, a proposal can offer a linked change for adoption or retention:

```json
{
  "options": ["adopt", "retain"],
  "recommendation": "adopt",
  "choice_effects": {"adopt": "accept", "retain": "keep_existing"}
}
```

## Retry and revise a human response

`decision.respond` validates the latest exact response evidence before every retry. Repeating the same explicit `source`, choice, source digest, quotation and quotation range is a no-op, including under a different RPC request ID; it adds no response event and leaves the review binding current. The RPC layer also replays an exact retry under the same request ID. To revise a choice—including changing `defer` to a final answer—register a new human source after the previous response. The same source may correct the quotation only while keeping the same choice. A newly executed call that omits `source` represents a new authenticated owner statement and registers a new source, even when the text repeats. If a client retries under a new request ID, it should pass the original source to identify an exact retry; reusing the source with a different choice is rejected.

For a provisional assumption, `expires` is the deadline for recording a final human choice. `defer` does not extend it. A final response event stores the controller-observed acceptance time; registering a source before the deadline alone does not count as an answer. At expiry, decision response, presentation and apply boundaries reconcile the decision in their writer transaction, close its old notice, publish one critical reconciliation notice and invalidate its references. A late response returns `status: "expired"` with `answered: false` and `must_reconcile: true`; the state change commits with that result. The assumption must be proposed again for a new answer. A final choice recorded before the deadline remains reviewable and applicable afterward. An exact retry and same-choice quote correction from the same source preserve that original timely choice and still require a fresh consistency review when the quote changes.

Choosing `keep_existing` normally leaves affected work fenced for reassessment. The controller may restore `validity: "current"` for a planned task only when its recorded pre-change fence proves there was no earlier block, plan, run, candidate, lease or execution attempt, and its task definition and accepted read pins remain unchanged. It keeps the incremented epoch and records a `task_revalidated_after_keep_existing` event. Ready, previously fenced, changed or ambiguous work remains on the usual reassessment path. A batch records only task proofs that survive all member effects and root invalidations; a task touched by another member remains fenced. The same proof and final task list appear in the atomic batch packet and its applied result.

Generic `inbox.acknowledge` cannot answer product or provisional decisions or resolve conflicts; use their bound decision workflow. A generic acknowledgement of an open notice records the exact notice version and human source. An exact retry of that human acknowledgement is idempotent only while the notice has not been automatically closed afterward. Automatic closure records `user_acknowledgement: false` and cannot be converted into a later human acknowledgement. Reissuing an identical notice reopens its existing ID and preserves its version; use a fresh human response source to acknowledge it again.

## Review and apply a decision batch

Each selected decision must already be in `decision_received` with a retained exact answer quotation anchored to a trusted human source. Prepare between two and twenty decisions in one project:

```json
{"project":"PRJ-...","decisions":["DEC-...","DEC-..."]}
```

Call `decision.batch_prepare` with that object. It freezes each proposal, answer and source digest, any linked change, policy update and supersession closure, and one shared baseline and projected final state. Overlapping artifact deltas and contradictory final constraints are rejected before a packet is stored.

Read the packet with `decision.batch_get` and `decision.batch_read`. The get response provides `read_digest` and `read_operation`. Pass that exact digest as `expected_digest`, start at `offset: 0`, and continue with each `next_offset` until it is `null`:

```json
{"batch":"DBATCH-...","expected_digest":"SHA256_FROM_GET","offset":0,"byte_budget":12000}
```

Offsets count Unicode characters; `byte_budget` limits UTF-8 bytes. Concatenate page `content` values and verify the SHA-256 of the UTF-8 bytes against `read_digest`. A returned page is not a review or approval. If review context names an external packet marker, the consistency reviewer must read the full packet and report that exact marker in `covered`.

Submit one consistency review through the normal review job route:

```json
{"kind":"review","args":{"subject":"DBATCH-...","role":"consistency","adapter":"REVIEWER"}}
```

Poll the returned job using `job.get` until it succeeds, then use `result.receipt` from that review. Apply the reviewed packet with:

```json
{"batch":"DBATCH-...","review_receipt":"RECEIPT-..."}
```

Call `decision.batch_apply` with this object. It rechecks the frozen source, answers, members and project baseline inside one writer transaction. A stale member or a failure while applying any member rolls the whole batch back. An applied batch can be replayed only with the exact review receipt already recorded for it.

The projected final state covers specifications, decisions, changes and explicitly scoped notice/blocker effects. It does not model every task-readiness field. Normal invalidation and work reassessment remain required after specification changes.

## Review one independent appended requirement

When an accepted, source-backed requirement is added after a full consistency PASS, the decision can use a bounded delta review if the controller proves that exactly one requirement was appended and every previously reviewed decision, answer, source, policy, invariant, linked change and accepted-artifact digest is unchanged. The new requirement must have no constraints, critical flag or trace links. A batch additionally proves that its immutable packet and every member binding differ only by that same append. Other changes, missing evidence, changed links, multiple additions or an oversized packet require the ordinary full consistency review.

Preview the option with `decision.review_subject` and `incremental_from` (or `decision.batch_get` with the same optional field for a batch). If it is available, submit a normal review job while setting `proposal` to `{"incremental_from":"BASE_RECEIPT"}`. The reviewer receives the frozen base material, the added requirement and its exact trusted human source text. The full base is retained as reference, so this first version reduces the review scope rather than the prompt size: the reviewer treats the base PASS as established, checks only the new requirement and its interaction with the prior final projection and invariants, and fails or blocks if that relationship is unclear. The supplemental PASS must cover each `decision-incremental-requirement:...` and `source-content:...` marker. Apply still uses the normal `decision.apply` or `decision.batch_apply` route.

The base receipt must remain the latest fully covered consistency PASS for the original material. The supplemental review uses the current full-material binding, so a later full or supplemental FAIL for that material supersedes it. Apply rebuilds the append-only proof and rechecks both receipt families, all coverage and current source/artifact snapshots. For a batch, the original immutable packet body and digest remain unchanged; its applied result records the base receipt, supplemental receipt, current material digest and reconstruction proof. Historical export checks that proof's structure and the frozen artifact, source and trace-link snapshots. Exported receipt IDs are historical references: archive validation does not authenticate live receipt judgments and cannot authorize a later apply.

## Apply a display-title repair without a second human answer

This route is limited to an accepted artifact whose canonical body changes only in `title`. Every other field must remain byte-for-byte equivalent after canonical JSON encoding; the delta must not withdraw the artifact. Title can carry product meaning, so this structural test only identifies a candidate repair. The original trusted user source and an independent latest consistency review still have to support semantic equivalence.

Create the change from the existing user instruction and exact artifact revision:

```json
{
  "project":"PRJ-...",
  "body":{
    "title":"Clarify the requirement label",
    "origin":"user",
    "reason":"Use the label requested in the source",
    "source":"SRC-...",
    "affected":["REQUIREMENT-..."],
    "evidence":["SRC-..."],
    "deltas":[{
      "artifact":"REQUIREMENT-...",
      "expected_revision":4,
      "body":{"title":"Clearer label","statement":"UNCHANGED", "acceptance":["UNCHANGED"], "source_refs":["UNCHANGED"]}
    }]
  }
}
```

Call `change.propose`, then obtain a feasibility review receipt with `job.submit` (`subject` is the change ID and `role` is `feasibility`). Record a bounded solution with `change.attempt`, citing the review receipt in `evidence` and setting `outcome` to `solution`. This moves the change to `reconciling`.

Submit a consistency review for the change. Its canonical context includes the complete before/after artifact, exact source text when it fits inline (otherwise a digest-bound `source.read` reference), and a `display-metadata-equivalence:ARTIFACT-ID` coverage marker. The reviewer must cover the marker, cite the artifact in an observation, and determine from the source that the title does not alter product meaning. Then call `change.apply` with the current consistency receipt. The application rechecks the current artifact, source trust, exact delta, latest receipt and marker coverage. A title-only diff by itself never grants semantic equivalence.

Changes to statements, acceptance criteria, constraints, interfaces or other behavior remain on the normal product-decision path. A title change combined with a withdrawal is also rejected by the technical-repair route.
