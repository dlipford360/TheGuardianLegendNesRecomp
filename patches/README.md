# Local Engine Patch

`nesrecomp.patch` is a generated diff against the exact pin in
`dependencies.json`. It contains UxROM corrections, the macOS/pinned-Mesen oracle
adapter, a compile-time co-simulation gate, and POSIX-relative scripted
screenshot paths. No game ROM or generated game
source is in this patch.

It also includes live vertical-address reload handling in the scanline renderer
for the bottom HUD split. `src/extras.c` opts this game into that renderer; other
projects retain their existing default-renderer selection.

`bash setup.sh` initializes recursive submodules and applies the patch after
checking its base revision. Already applied patches are left alone; incompatible
local edits cause a failure rather than a reset. A modified `nesrecomp` submodule
is expected in this bootstrap repository.

After intentionally editing tracked engine source and validating it:

```bash
python3 tools/project.py export-patches
```

Review the resulting patch before including it in any future commit. This helper
does not export untracked engine files. No patch or submodule commit has been
pushed upstream. Once a maintained engine fork exists, the patch can be replaced
by a reachable pinned commit; do not point a gitlink at an unpublished local
commit that collaborators cannot fetch.
