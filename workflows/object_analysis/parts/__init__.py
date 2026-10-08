"""The pieces the object_analysis steps are built from.

A step file should read like a recipe card: what goes in, what comes out,
and the few calls that do the work. The work itself lives here, one module
per concern, so that a reader looking for how the border filter decides
what to drop, or which Cellpose device was used, finds it in a file named
for it rather than in the middle of a long step.

- ``settings.py``       reads the detection settings from the submit and the
                        recipe, and makes the hash that identifies a segmentation
- ``masks.py``          filters and resizes label images
- ``watershed.py``      the fast detector, a watershed without a model
- ``cellpose_model.py`` loads Cellpose once per worker, on the best device
- ``checkpoint.py``     files the masks and a record of how they were made
- ``contract.py``       the shape of the object table every reader relies on

A step imports them after putting ``workflows/`` on Python's search path,
as it does for ``shared``::

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from object_analysis.parts.masks import filter_masks_by_area
"""
