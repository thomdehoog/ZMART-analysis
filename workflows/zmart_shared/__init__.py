"""Helpers that more than one workflow's steps use.

The name carries the project in it on purpose: a plain ``shared`` could be
confused with another package of that name in a step's environment, and
Python keeps the first one it imported.

- ``image_io.py`` reads a plane from an OME-Zarr position or an OME-TIFF.
- ``focus_metrics.py`` holds the sharpness measures (Brenner, DCT entropy,
  Vollath F4 and brightest-pixel intensity).
- ``population.py`` describes a population of objects: which columns count
  as measurements, how they are scaled, and their principal components.

A step imports them after putting ``workflows/`` on Python's search path::

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from zmart_shared.image_io import load_plane

Keeping one copy means a fix reaches every step that uses it.
"""
