# Changelog

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
