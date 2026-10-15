"""
Handover -- how one step's output reaches the next, through a folder.

A step's output is written to a folder of its own: every NumPy array as a
``.npy`` file, and the rest as one pickle in which each array is a small
marker. The next step reads it back. Arrays never pass through the engine,
so the engine needs no NumPy, and a step in one NumPy version can hand an
array to a step in another.

Standard library only. The worker script imports this module by file, in
whatever environment a step runs in; the engine imports it as part of the
package. NumPy is used only where an array already exists, or where a
marker says one was written.
"""

import os
import pickle
import sys
from collections.abc import Mapping, Sequence

PICKLE_PROTOCOL = 5

#: The key of a marker that stands for an array, and of one that stands
#: for a scoped step's results, which stay folders.
ARRAY = "zmart.npy"
RESULTS = "zmart.results"


def write_folder(data, folder):
    """Write *data* into *folder*: arrays as ``.npy``, the rest pickled."""
    os.makedirs(folder, exist_ok=True)
    np = sys.modules.get("numpy")
    count = 0

    def strip(value):
        nonlocal count
        if np is not None and isinstance(value, np.ndarray) and value.dtype != object:
            name = f"array_{count}.npy"
            count += 1
            np.save(os.path.join(folder, name), value, allow_pickle=False)
            return {ARRAY: name}
        if np is not None and isinstance(value, np.generic):
            return value.item()
        if isinstance(value, StoredResults):
            return {RESULTS: value.folders}
        if type(value) is dict:
            return {strip(key): strip(item) for key, item in value.items()}
        if type(value) is list:
            return [strip(item) for item in value]
        if type(value) is tuple:
            return tuple(strip(item) for item in value)
        return value

    with open(os.path.join(folder, "data.pickle"), "wb") as f:
        pickle.dump(strip(data), f, protocol=PICKLE_PROTOCOL)


def read_folder(folder):
    """Read what :func:`write_folder` wrote, arrays loaded back in."""
    with open(os.path.join(folder, "data.pickle"), "rb") as f:
        data = pickle.load(f)

    def restore(value):
        if type(value) is dict:
            if len(value) == 1 and ARRAY in value:
                import numpy

                return numpy.load(os.path.join(folder, value[ARRAY]), allow_pickle=False)
            if len(value) == 1 and RESULTS in value:
                return StoredResults(value[RESULTS])
            return {key: restore(item) for key, item in value.items()}
        if type(value) is list:
            return [restore(item) for item in value]
        if type(value) is tuple:
            return tuple(restore(item) for item in value)
        return value

    return restore(data)


def summary_of(result):
    """The few things the engine keeps of an output: enough for a lineage."""
    if not isinstance(result, dict):
        return {}
    metadata = result.get("metadata") or {}
    return {
        "lineage": result.get("lineage"),
        "provenance": result.get("provenance") or {},
        "metadata": {"submission_idx": metadata.get("submission_idx")},
    }


class StoredResults(Sequence):
    """A scoped step's ``pipeline_data["results"]``: one output per entry,
    read from its folder when the step reaches it, and not kept."""

    def __init__(self, folders):
        self.folders = list(folders)

    def __len__(self):
        return len(self.folders)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [read_folder(folder) for folder in self.folders[index]]
        return read_folder(self.folders[index])

    def __repr__(self):
        return f"<{len(self)} results>"


class Output(Mapping):
    """A done job's output on the board, read from its folder when first
    looked into."""

    def __init__(self, folder):
        self.folder = folder
        self.data = None

    def loaded(self):
        if self.data is None:
            self.data = read_folder(self.folder)
        return self.data

    def __getitem__(self, key):
        return self.loaded()[key]

    def __iter__(self):
        return iter(self.loaded())

    def __len__(self):
        return len(self.loaded())

    def __repr__(self):
        return repr(self.loaded())
