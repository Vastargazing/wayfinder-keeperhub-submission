# SDK patches

Base: `46dbf7c05e7f17e6c10a136da0dae6e13e590e95` from
https://github.com/WayfinderFoundation/wayfinder-paths-sdk . Apply in order:

1. `wayfinder-external-executor-seam.patch`
2. `wayfinder-base-sepolia.patch`
3. `wayfinder-durable-execution.patch`

All three patch bytes are unchanged from the project baseline. The seam
corresponds to SDK `197de649fa386407cb99f1a38c018c95ae1944ba`; the Base Sepolia
layer corresponds to `75d8b9b4053f7e9a75f5577a42cf80ab00b5d41a`. The final durable
layer adds original-operation journal binding, named steps, claim validation and
fresh permission invalidation. `reconstruction.json` pins all eight affected
files, including tests; the checker fails on missing, extra or mismatched targets.

`prepare_sdk_snapshot.py` builds a separate SDK using local Git objects.
`check_sdk_snapshot.py` independently reconstructs and tests it. Neither checks
KeeperHub server source or establishes a hosted deployment. See
[reproduction](../docs/reproduction.md) and [command map](../docs/command-map.md).
The retained [Wayfinder license](../third-party/Wayfinder-LICENSE.txt) applies to
upstream material; original project licensing is pending owner decision.
