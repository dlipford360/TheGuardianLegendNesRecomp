# Guardian Legend port review

This branch reconstructs reviewable ancestry from the source revision recorded
in `dependencies.json`: `9152ffe93004c4b3c060751b18bc0b333b32869f` of
`mstan/SuperMarioBrosNESRecomp`.

The original local two-commit history remains on `backup/pre-history-repair`.
These are reconstructed topic commits, not recovered original development
commits. The first commit removes Mario-specific material; subsequent commits
configure Guardian Legend, add regression tooling, and document development.

Before adding this note, the final tree was compared with `eccbd21`: the only
difference was removal of the tracked `guardian-legend.log` runtime artifact.
The engine remains pinned and reproduced by `patches/nesrecomp.patch`; the
working engine patch reverse-check passed. No new runtime behavior is
introduced by this history reconstruction.

This is a standalone game-port review, not a proposal to replace the Mario
game upstream. Two separate engine proposals target current NESRecomp:
`review/macos-compatibility` and `review/guardian-legend-mapper2`.
Those are draft rebases and have their own validation notes.
