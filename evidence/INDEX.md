# Evidence index

Claims below are bounded by the available artifacts. Runtime reports are produced
by the documented commands; they are not raw live receipts. Snapshot revision
and file hashes are in [SOURCE-PROVENANCE.json](../SOURCE-PROVENANCE.json).

| Claim / mode | Available source | Revision or time | Boundary |
|---|---|---|---|
| SOURCE: persisted operation, ownership and unknown-result refusal | `integration/tests/test_authorization.py`, `test_sdk_contract.py`, SDK patches | Baseline 5d41778 and exact SDK recipe | Source assertions; running tests adds local/model observation |
| Local runtime/model: recovery and console refusals | `judge_checks.py` commands/JUnit/origins, generated in WORK/results | This extracted snapshot at run time | Synthetic HTTP/RPC, real SDK/journal; no EVM/Turnkey |
| Model negative controls | `offline_mutations.py`, generated 26 control/mutant pairs | This extracted snapshot at run time | Detection requires matching testcase identities and assertion failures, not timeouts |
| Model UI/status | `snapshot_user_path.py`, generated WORK/user-path | This extracted snapshot at run time | Real producer/consumer with synthetic Chain; no RPC |
| HISTORICAL hosted mint result consumed | [redacted summary](historical-mint-summary.json) and [Base Sepolia transaction](https://sepolia.basescan.org/tx/0x27a8a3847017b5dac5e91c41f1854e73cf92abff5bbc000e574f50b0e4108c63) | 2026-09-09T11:05:50.970909+00:00 to 2026-09-09T11:05:59.137710+00:00; historical project head in summary | Summary derived from accepted runtime record, not raw receipt; source hashes retained. Explorer not freshly checked here. Mint is not supply/withdraw; goalVerified=false |
| HISTORICAL fork demonstrations | GAP: historical stand/evidence not distributed | Earlier local work; no fresh run | No fork runtime claim from this archive's offline tests |
| Public tuple/overload contribution | [PR #2349](https://github.com/KeeperHub/keeperhub/pull/2349), [revision record](../docs/submission-evidence.md) | Final PR head and merge recorded there | Separate bounty; merge does not establish deployed hosted version |
| New live Aave cycle/current-code live crash recovery | UNVERIFIED | No witness in this candidate | No completed cycle claimed |

## Redaction

The historical summary retains original timestamps, status, transaction hash,
selected counters and consumption state. It omits private identifiers, credential
metadata, detailed execution records and local paths. Its hashes bind the private
source records for provenance, without promising access to those records. The
summary alone cannot independently replay their full verification. It is clearly
a derived artifact, and it retains the incomplete-strategy result.

## Legacy comments and evidence gaps

Unchanged product files retain historical `research/...` path strings in source
comments and labels. Those local reports are not supplied or accessible evidence.
They are historical provenance references, not inputs to the supported commands.
For current behavior inspect the distributed implementation/tests and reproduce
the snapshot suite. Earlier fork/stand claims in those comments are GAP in this
candidate. A public upstream path cited in the hosted verifier describes its
execution profile; it is not a claim that this snapshot verifies server source.
