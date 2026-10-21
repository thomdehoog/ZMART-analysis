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
#: for a scoped step's parts, which stay folders.
ARRAY = "zmart.npy"
PARTS = "zmart.parts"

#: The keys the engine writes into an output. They are not part of what a
#: part hands on as ``output``.
ENGINE_KEYS = ("input", "metadata", "provenance", "lineage")


def write_folder(data, folder):
    """Write *data* into *folder*: arrays as ``.npy``, the rest pickled."""
    os.makedirs(folder, exist_ok=True)
    np = sys.modules.get("numpy")
    count = 0

    def strip(value):
        nonlocal count
        if np is not None and type(value) is np.ndarray and value.dtype != object:
            name = f"array_{count}.npy"
            count += 1
            np.save(os.path.join(folder, name), value, allow_pickle=False)
            return {ARRAY: name}
        if np is not None and isinstance(value, np.generic):
            return value.item()
        if isinstance(value, Parts):
            return {PARTS: value.refs}
        if type(value) is dict:
            return {strip(key): strip(item) for key, item in value.items()}
        if type(value) is list:
            return [strip(item) for item in value]
        if type(value) is tuple:
            return tuple(strip(item) for item in value)
        return value

    with open(os.path.join(folder, "data.pickle"), "wb") as f:
        pickle.dump(strip(data), f, protocol=PICKLE_PROTOCOL)


def read_folder(folder, lists=False, read=None):
    """Read what :func:`write_folder` wrote, arrays loaded back in.

    A scoped step's parts stay folders, read one at a time; with *lists*
    they are read now, into a list. *read* keeps what was read by folder,
    so a folder that several outputs hold is read once.
    """
    if read is not None and folder in read:
        return read[folder]
    with open(os.path.join(folder, "data.pickle"), "rb") as f:
        data = pickle.load(f)

    def restore(value):
        if type(value) is dict:
            if len(value) == 1 and ARRAY in value:
                import numpy

                return numpy.load(os.path.join(folder, value[ARRAY]), allow_pickle=False)
            if len(value) == 1 and PARTS in value:
                if lists:
                    return [part_of(ref, read) for ref in value[PARTS]]
                return Parts(value[PARTS])
            return {key: restore(item) for key, item in value.items()}
        if type(value) is list:
            return [restore(item) for item in value]
        if type(value) is tuple:
            return tuple(restore(item) for item in value)
        return value

    data = restore(data)
    if read is not None:
        read[folder] = data
    return data


def summary_of(result):
    """The few things the engine keeps of an output: enough for a lineage."""
    if not isinstance(result, dict):
        return {}
    return {"lineage": result.get("lineage"), "provenance": result.get("provenance") or {}}


def part_of(ref, read=None):
    """The part a stored output is, from its ``(folder, id, scope)``.

    ``id`` and ``scope`` come from the engine, which knows them; ``output``
    is what the steps left, without the engine's own keys. An image part
    also carries the ``input`` it was submitted with, when the steps kept it.
    """
    folder, job, scope = ref
    data = read_folder(folder, read is not None, read)
    part = {
        "id": job,
        "scope": dict(scope),
        "output": {key: value for key, value in data.items() if key not in ENGINE_KEYS},
    }
    if "input" in data:
        part["input"] = data["input"]
    return part


class Parts(Sequence):
    """A scoped step's ``pipeline_data["parts"]``: one part per entry, read
    from its folder when the step reaches it, and not kept."""

    def __init__(self, refs):
        self.refs = [tuple(ref) for ref in refs]

    def __len__(self):
        return len(self.refs)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [part_of(ref) for ref in self.refs[index]]
        return part_of(self.refs[index])

    def __repr__(self):
        return f"<{len(self)} parts>"


class Output(Mapping):
    """A done job's output on the board, read from its folder when first
    looked into."""

    def __init__(self, folder):
        self.folder = folder
        self.data = None

    def loaded(self):
        if self.data is None:
            self.data = read_folder(self.folder, lists=True)
        return self.data

    def __getitem__(self, key):
        return self.loaded()[key]

    def __iter__(self):
        return iter(self.loaded())

    def __len__(self):
        return len(self.loaded())

    def __repr__(self):
        return repr(self.loaded())
