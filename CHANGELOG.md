# Changelog

## 0.6.0rc10 (2026-11-03) -- the example readers read OME-TIFF positions fully

A ZMART run can now be saved as OME-TIFF as well as OME-Zarr, and the
example workflows read both alike. Three fixes to their readers, each with a
test; the engine is unchanged from 0.6.0rc9.

- **An OME-TIFF that keeps smaller copies of its picture inside it** (as Fiji,
  QuPath and ZMART's positions write it) is now read at full size. Both
  readers took the first level's own view, which for such a file holds every
  size rather than one picture, and failed with "'Group' object has no
  attribute 'shape'".
- **`object_analysis` gives an OME-TIFF's origin with its height**: the
  height of the first plane of the stack, as an OME-Zarr position's origin
  gives it, whichever plane was read.
- **`focus` numbers an OME-TIFF stack's plane heights from where they were
  taken**, from the first plane's own height rather than from zero, so a
  focus found on an OME-TIFF stack lands at the right height.

## 0.6.0rc9 (2026-11-02) -- release candidate for the first public release

- The engine, `zmart_analysis`: register a pipeline, submit images as they
  come in, and read the results from the job board.
- Each step declares its scope (an image, a group, a compartment, a carrier
  or the experiment) and runs as soon as the images for that scope are in.
- Each step runs in its own conda environment, described with an
  environment file and a lock file, so conflicting dependencies do not meet.
- Workers stay active and run steps concurrently.
- Two example workflows in `examples/`: `focus` and `object_analysis`. They
  are there to read and copy from and are not installed with the engine.
- Documentation in `docs/`: part 1, use the engine; part 2, implement an
  analysis step.
