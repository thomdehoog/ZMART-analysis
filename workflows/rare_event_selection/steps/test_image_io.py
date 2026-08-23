"""
Unit tests for image_io.

Builds synthetic OME-Zarr positions in both NGFF 0.4 (Zarr v2) and NGFF
0.5 (Zarr v3, sharded) and checks that planes, metadata and physical
coordinates come back the same either way.

Run from an environment with ngio installed:
    python test_image_io.py
    python -m pytest test_image_io.py -v
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from image_io import is_ome_zarr, load_plane, to_physical


PIXEL_SIZE = 0.325
Z_SPACING = 1.0
ORIGIN_YX = (1000.0, 2000.0)
SHAPE = (2, 2, 5, 64, 64)  # t, c, z, y, x


def _make_position(path, ngff_version, array, shards=None):
    """Write one synthetic OME-Zarr position."""
    import ngio

    ngio.create_ome_zarr_from_array(
        path,
        array,
        pixelsize=PIXEL_SIZE,
        z_spacing=Z_SPACING,
        axes_names=("t", "c", "z", "y", "x"),
        channels_meta=["DAPI", "GFP"],
        translation=(0.0, 0.0, 0.0, ORIGIN_YX[0], ORIGIN_YX[1]),
        levels=2,
        chunks=(1, 1, 1, 32, 32),
        shards=shards,
        ngff_version=ngff_version,
        overwrite=True,
    )


class OmeZarrTestCase(unittest.TestCase):
    """Shared fixtures: one position per NGFF version."""

    @classmethod
    def setUpClass(cls):
        try:
            import ngio  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("ngio is not installed")

        cls.tmpdir = Path(tempfile.mkdtemp(prefix="image_io_test_"))
        rng = np.random.default_rng(0)
        cls.array = rng.integers(0, 4096, SHAPE, dtype=np.uint16)

        cls.stores = {
            "0.4": cls.tmpdir / "position_v04.zarr",
            "0.5": cls.tmpdir / "position_v05.zarr",
        }
        _make_position(cls.stores["0.4"], "0.4", cls.array)
        # NGFF 0.5 is Zarr v3, so it can also carry shards.
        _make_position(cls.stores["0.5"], "0.5", cls.array,
                       shards=(1, 1, 1, 64, 64))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)


class TestDetection(OmeZarrTestCase):
    """Tests for is_ome_zarr()."""

    def test_detects_v04_store(self):
        # NGFF 0.4 keeps its metadata in .zattrs
        self.assertTrue((self.stores["0.4"] / ".zattrs").exists())
        self.assertTrue(is_ome_zarr(self.stores["0.4"]))

    def test_detects_v05_store(self):
        # NGFF 0.5 keeps its metadata in zarr.json
        self.assertTrue((self.stores["0.5"] / "zarr.json").exists())
        self.assertTrue(is_ome_zarr(self.stores["0.5"]))

    def test_rejects_plain_file(self):
        plain = self.tmpdir / "image.tif"
        plain.write_bytes(b"not a zarr")
        self.assertFalse(is_ome_zarr(plain))

    def test_remote_url_by_suffix(self):
        self.assertTrue(is_ome_zarr("s3://bucket/plate.zarr/B/03/0"))
        self.assertFalse(is_ome_zarr("s3://bucket/image.tif"))


class TestPlaneSelection(OmeZarrTestCase):
    """Both NGFF versions must yield identical planes."""

    def test_default_takes_middle_z(self):
        for version, store in self.stores.items():
            with self.subTest(ngff=version):
                plane, meta = load_plane(store)
                np.testing.assert_array_equal(plane, self.array[0, 0, 2])
                self.assertEqual(meta["index"], {"t": 0, "z": 2})

    def test_explicit_indices(self):
        for version, store in self.stores.items():
            with self.subTest(ngff=version):
                plane, meta = load_plane(store, t=1, c=1, z=4)
                np.testing.assert_array_equal(plane, self.array[1, 1, 4])
                self.assertEqual(meta["index"], {"t": 1, "z": 4})
                self.assertEqual(meta["channel"], 1)

    def test_channel_by_name(self):
        for version, store in self.stores.items():
            with self.subTest(ngff=version):
                plane, meta = load_plane(store, c="GFP", z=0)
                np.testing.assert_array_equal(plane, self.array[0, 1, 0])
                self.assertEqual(meta["channel"], 1)
                self.assertEqual(meta["channel_name"], "GFP")

    def test_max_projection(self):
        for version, store in self.stores.items():
            with self.subTest(ngff=version):
                plane, meta = load_plane(store, z="max")
                np.testing.assert_array_equal(plane, self.array[0, 0].max(axis=0))
                self.assertEqual(meta["projection"], "max")
                self.assertNotIn("z", meta["index"])

    def test_projection_keeps_dtype(self):
        for version, store in self.stores.items():
            with self.subTest(ngff=version):
                for mode in ("max", "mean"):
                    plane, _ = load_plane(store, z=mode)
                    self.assertEqual(plane.dtype, self.array.dtype)

    def test_plane_is_always_2d(self):
        for version, store in self.stores.items():
            with self.subTest(ngff=version):
                for z in (0, "mid", "max"):
                    plane, _ = load_plane(store, z=z)
                    self.assertEqual(plane.ndim, 2)

    def test_lower_resolution_level(self):
        for version, store in self.stores.items():
            with self.subTest(ngff=version):
                plane, meta = load_plane(store, level=1)
                self.assertEqual(plane.shape, (32, 32))
                self.assertEqual(meta["level"], "1")
                self.assertAlmostEqual(meta["pixel_size"]["x"], PIXEL_SIZE * 2)

    def test_out_of_range_index_raises(self):
        with self.assertRaises(Exception):
            load_plane(self.stores["0.5"], t=99)


class TestMetadata(OmeZarrTestCase):
    """Metadata is normalized across NGFF versions."""

    def test_reports_ngff_version(self):
        for version, store in self.stores.items():
            with self.subTest(ngff=version):
                _, meta = load_plane(store)
                self.assertEqual(meta["ngff_version"], version)
                self.assertEqual(meta["format"], "ome-zarr")

    def test_reports_axes_and_shape(self):
        for version, store in self.stores.items():
            with self.subTest(ngff=version):
                _, meta = load_plane(store)
                self.assertEqual(meta["axes"], ["t", "c", "z", "y", "x"])
                self.assertEqual(meta["shape"], list(SHAPE))
                self.assertEqual(meta["n_levels"], 2)

    def test_reports_pixel_size_and_origin(self):
        for version, store in self.stores.items():
            with self.subTest(ngff=version):
                _, meta = load_plane(store)
                self.assertAlmostEqual(meta["pixel_size"]["y"], PIXEL_SIZE)
                self.assertAlmostEqual(meta["pixel_size"]["z"], Z_SPACING)
                self.assertAlmostEqual(meta["origin"]["y"], ORIGIN_YX[0])
                self.assertAlmostEqual(meta["origin"]["x"], ORIGIN_YX[1])
                self.assertEqual(meta["space_unit"], "micrometer")


class TestPhysicalCoordinates(OmeZarrTestCase):
    """Pixel centroids map to stage coordinates."""

    def test_scale_and_offset_applied(self):
        _, meta = load_plane(self.stores["0.5"])
        physical = to_physical(10.0, 20.0, meta)
        self.assertAlmostEqual(physical["y"], 10.0 * PIXEL_SIZE + ORIGIN_YX[0])
        self.assertAlmostEqual(physical["x"], 20.0 * PIXEL_SIZE + ORIGIN_YX[1])
        self.assertEqual(physical["unit"], "micrometer")

    def test_uses_the_loaded_level(self):
        _, meta = load_plane(self.stores["0.5"], level=1)
        physical = to_physical(10.0, 20.0, meta)
        self.assertAlmostEqual(physical["y"], 10.0 * PIXEL_SIZE * 2 + ORIGIN_YX[0])

    def test_none_without_spatial_metadata(self):
        self.assertIsNone(to_physical(1.0, 2.0, {"pixel_size": {}}))


class TestLazyReading(OmeZarrTestCase):
    """Only the chunks backing the requested plane are fetched."""

    METADATA_KEYS = (".zarray", ".zattrs", ".zgroup", ".zmetadata", "zarr.json")

    def _count_chunk_reads(self, **kwargs):
        """Number of distinct chunk or shard objects touched by one load."""
        from unittest.mock import patch
        from zarr.storage import LocalStore

        original = LocalStore.get
        reads = set()

        async def counting_get(self, key, *args, **kw):
            # A sharded read hits one object with several byte ranges,
            # so count distinct keys rather than calls.
            if key.rsplit("/", 1)[-1] not in counting_get.METADATA_KEYS:
                reads.add(key)
            return await original(self, key, *args, **kw)

        counting_get.METADATA_KEYS = self.METADATA_KEYS

        with patch.object(LocalStore, "get", counting_get):
            load_plane(**kwargs)

        return len(reads)

    def test_single_plane_reads_one_chunk_per_tile(self):
        # Level 0 holds 2*2*5 z-planes of 2x2 chunks: 80 chunks in all.
        # One plane is 4 of them, or a single shard when sharded.
        for version, store in self.stores.items():
            with self.subTest(ngff=version):
                reads = self._count_chunk_reads(source=store, t=0, c=0, z=2)
                self.assertLessEqual(
                    reads, 4,
                    f"read {reads} chunks for a single plane, expected <= 4",
                )

    def test_projection_reads_only_its_own_stack(self):
        # A z-projection needs the 5 z planes of one (t, c), not all 20.
        for version, store in self.stores.items():
            with self.subTest(ngff=version):
                reads = self._count_chunk_reads(source=store, t=0, c=0, z="max")
                self.assertLessEqual(
                    reads, 20,
                    f"read {reads} chunks for one z-stack of 5 planes, "
                    f"expected <= 20 of the 80 in the array",
                )


class TestAxisVariants(OmeZarrTestCase):
    """Positions are not always 5D: t, c and z may all be absent."""

    def _write(self, name, axes, shape):
        import ngio

        store = self.tmpdir / name
        array = np.random.default_rng(1).integers(0, 4096, shape, dtype=np.uint16)
        ngio.create_ome_zarr_from_array(
            store, array, pixelsize=PIXEL_SIZE, axes_names=axes,
            ngff_version="0.5", levels=1, overwrite=True,
        )
        return store, array

    def test_czyx(self):
        store, array = self._write("czyx.zarr", ("c", "z", "y", "x"), (2, 3, 32, 32))
        plane, meta = load_plane(store, c=1)
        np.testing.assert_array_equal(plane, array[1, 1])
        self.assertEqual(meta["index"], {"z": 1})

    def test_zyx(self):
        store, array = self._write("zyx.zarr", ("z", "y", "x"), (3, 32, 32))
        plane, _ = load_plane(store, z="max")
        np.testing.assert_array_equal(plane, array.max(axis=0))

    def test_yx(self):
        store, array = self._write("yx.zarr", ("y", "x"), (32, 32))
        plane, meta = load_plane(store)
        np.testing.assert_array_equal(plane, array)
        self.assertEqual(meta["index"], {})

    def test_tyx_without_z(self):
        store, array = self._write("tyx.zarr", ("t", "y", "x"), (4, 32, 32))
        plane, meta = load_plane(store, t=3)
        np.testing.assert_array_equal(plane, array[3])
        self.assertEqual(meta["index"], {"t": 3})


class TestOtherInputs(unittest.TestCase):
    """Image files and skimage samples still work."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="image_io_files_"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_reads_a_tiff(self):
        from skimage.io import imsave

        path = self.tmpdir / "plane.tif"
        image = np.random.default_rng(2).integers(0, 255, (16, 16), dtype=np.uint8)
        imsave(path, image, check_contrast=False)

        plane, meta = load_plane(path)
        np.testing.assert_array_equal(plane, image)
        self.assertEqual(meta["format"], "image-file")

    def test_image_file_has_no_physical_coordinates(self):
        from skimage.io import imsave

        path = self.tmpdir / "plane2.tif"
        imsave(path, np.zeros((8, 8), dtype=np.uint8), check_contrast=False)

        _, meta = load_plane(path)
        self.assertEqual(meta["pixel_size"], {})
        self.assertIsNone(to_physical(1.0, 2.0, meta))

    def test_rejects_a_3d_image_file(self):
        from skimage.io import imsave

        path = self.tmpdir / "stack.tif"
        imsave(path, np.zeros((3, 8, 8), dtype=np.uint8), check_contrast=False)

        with self.assertRaises(ValueError):
            load_plane(path)

    def test_unknown_skimage_sample(self):
        with self.assertRaises(ValueError):
            load_plane("skimage.not_a_dataset")


class TestPlateInput(OmeZarrTestCase):
    """A plate is not a position, and the error should say so."""

    def test_plate_error_lists_positions(self):
        import ngio

        store = self.tmpdir / "plate.zarr"
        ngio.create_empty_plate(
            store, name="plate",
            images=[ngio.ImageInWellPath(row="B", column="3", path="0")],
            ngff_version="0.5",
        )

        with self.assertRaises(ValueError) as caught:
            load_plane(store)

        message = str(caught.exception)
        self.assertIn("not a position", message)
        self.assertIn("B/03/0", message)


class TestSteps(OmeZarrTestCase):
    """The pipeline steps consume an OME-Zarr position end to end."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            import skimage  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("scikit-image is not installed")

    @staticmethod
    def _load_step(name):
        """Load a step file the way the engine does: by path, not by import."""
        import types

        path = Path(__file__).parent / f"{name}.py"
        namespace = {"__name__": name, "__file__": str(path)}
        exec(compile(path.read_text(), str(path), "exec"), namespace)
        module = types.ModuleType(name)
        module.__dict__.update(namespace)
        return module

    def _pipeline_data(self, store):
        return {
            "metadata": {"label": "position_A1", "verbose": 0},
            "input": {"data_source": str(store)},
        }

    def test_preprocess_reads_a_position(self):
        preprocess = self._load_step("preprocess")
        data = preprocess.run(self._pipeline_data(self.stores["0.5"]),
                              t=1, c="GFP", z="max", sigma=1.0)

        result = data["preprocess"]
        np.testing.assert_array_equal(result["image"],
                                      self.array[1, 1].max(axis=0))
        self.assertEqual(result["image_preprocessed"].shape, (64, 64))
        self.assertEqual(result["image_metadata"]["channel_name"], "GFP")
        self.assertEqual(result["image_metadata"]["ngff_version"], "0.5")

    def test_feedback_writes_physical_coordinates(self):
        preprocess = self._load_step("preprocess")
        extract = self._load_step("extract_features")
        feedback = self._load_step("feedback")

        data = preprocess.run(self._pipeline_data(self.stores["0.4"]), z="mid")

        from skimage.measure import label
        image = data["preprocess"]["image_preprocessed"]
        masks = label(image > image.mean())
        data["segment"] = {"masks": masks, "n_cells": int(masks.max())}

        data = extract.run(data, select_by="area", percentile=90)
        data = feedback.run(data, output_dir=str(self.tmpdir / "output"))

        written = json.loads(Path(data["feedback"]["filepath"]).read_text())
        self.assertEqual(written["image"]["ngff_version"], "0.4")
        self.assertEqual(written["image"]["source"], str(self.stores["0.4"]))

        self.assertTrue(written["cells"], "expected at least one cell")
        for cell, source in zip(written["cells"], data["feedback"]["cells"]):
            self.assertEqual(cell["physical_unit"], "micrometer")
            self.assertAlmostEqual(
                cell["centroid_x_physical"],
                source["centroid_x"] * PIXEL_SIZE + ORIGIN_YX[1],
            )
            self.assertAlmostEqual(
                cell["centroid_y_physical"],
                source["centroid_y"] * PIXEL_SIZE + ORIGIN_YX[0],
            )

    def test_feedback_omits_physical_coordinates_without_metadata(self):
        feedback = self._load_step("feedback")

        data = {
            "metadata": {"label": "no_metadata", "verbose": 0},
            "preprocess": {"image_metadata": {"pixel_size": {}}},
            "segment": {"n_cells": 1},
            "extract_features": {
                "properties": {
                    "label": np.array([1]),
                    "area": np.array([10]),
                    "centroid-0": np.array([1.0]),
                    "centroid-1": np.array([2.0]),
                    "mean_intensity": np.array([3.0]),
                    "eccentricity": np.array([0.5]),
                },
                "selected_labels": np.array([1]),
                "threshold": 10.0,
                "select_by": "area",
                "percentile": 90,
            },
        }
        data = feedback.run(data, output_dir=str(self.tmpdir / "output2"))

        cell = data["feedback"]["cells"][0]
        self.assertNotIn("centroid_x_physical", cell)


if __name__ == "__main__":
    unittest.main()
