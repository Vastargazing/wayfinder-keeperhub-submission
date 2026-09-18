# Submission evidence

## KeeperHub PR #2349 — merged, 2026-09-10

**OBSERVED:** GitHub's PR API reported `MERGED` during the 2026-09-10
check. This is a revision-scoped record, not a claim about hosted deployment.

- PR: [#2349 — expand tuples in overloaded ABI function keys](https://github.com/KeeperHub/keeperhub/pull/2349).
- Author: `Vastargazing`.
- Target branch: `staging`.
- Final PR head: `0e17e270cf8edfab52daccf0b863392388aa1a74`.
- Merge commit: [`9695fa6b95b1dc6bc930810e03789c5f4d9dc5c1`](https://github.com/KeeperHub/keeperhub/commit/9695fa6b95b1dc6bc930810e03789c5f4d9dc5c1).
- Merged at: `2026-09-10T02:09:35Z`, by `suisuss`.
- GitHub diff totals: 32 files, 2,507 additions, 201 deletions. These describe
  scope; they are not a quality metric.

**SOURCE — final PR description:** the contribution expands tuple types in
canonical function keys, resolves unambiguous saved legacy keys, rejects
ambiguous selections, preserves healthy ABI entries when another is malformed,
uses the resolved signature for simulation encoding and decoding, and fixes
selector deduplication when combining Diamond ABIs. Ambiguous saved keys and
bare names require an explicit function selection; that compatibility change
is documented in the PR.

The final description reports 369 regression tests across 19 files at the final
head, plus type-check and formatting checks. These are the author's reported
checks, not a fresh independent test run in this documentation task. Earlier
test totals from other heads must not be presented as validation of this head.

## How to use this in the two submissions

**Bounty:** use the merged PR as the central contribution in a separate BUIDL.
Show the original tuple/legacy-key problem, the final positive behavior, and
the refusal of an ambiguous selection. Compare the original PR base
`e089f84356c5d31322b3a89299fe97a9c76ffd4c` with the final head. Regressions
introduced and repaired during PR review must not be attributed to that base.

**Main track:** cite the accepted upstream contribution as supporting evidence
of integration work. It does not replace the working Wayfinder integration,
its video, or transaction evidence. The historical approve and mint
transactions do not demonstrate the tuple/overload fix.

**SOURCE — organizer reply supplied by the owner in this conversation:** a
transaction is not mandatory for the bounty, and a PR need only be ready by the
submission deadline, not merged. Keep this clarification scoped to the bounty.
The separate bounty BUIDL and demonstration still need to be prepared; merge
alone does not complete submission or guarantee an award.

**UNVERIFIED:** deployment of this merge to hosted KeeperHub. Merge into
`staging` does not establish deployment to production.

Suggested submission wording:

> While building our Wayfinder Paths integration, we contributed tuple and
> overloaded-function handling fixes to KeeperHub. PR #2349 was reviewed and
> merged into KeeperHub's staging branch.

Before submitting, attach the PR link and before/after demonstration, check
the actual BUIDL requirements, and keep the two submissions' evidence separate.
This record does not authorize publication or submission.
