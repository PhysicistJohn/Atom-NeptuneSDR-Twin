import json
import math
import os
from pathlib import Path
import unittest

from neptunesdr_twin.fft import FFTWindow, radix2_fft, window_coefficients


def _vector_path():
    configured_root = os.environ.get("ATOM_DSP_ROOT")
    dsp_root = (
        Path(configured_root).expanduser().resolve()
        if configured_root
        else Path(__file__).resolve().parents[2] / "Atom-DSP"
    )
    return dsp_root / "vectors" / "dsp-conformance-v1.json"


class AtomDspConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = _vector_path()
        if not path.is_file():
            raise RuntimeError(
                "Atom-DSP conformance vectors are required; set ATOM_DSP_ROOT "
                "or place Atom-DSP beside Atom-NeptuneSDR-Twin"
            )
        cls.vectors = json.loads(path.read_text(encoding="utf-8"))
        if cls.vectors.get("schemaVersion") != "dsp-conformance-v1":
            raise RuntimeError("unsupported Atom-DSP conformance-vector schema")

    def assert_sequence_close(self, actual, expected, tolerance):
        self.assertEqual(len(actual), len(expected))
        for index, (actual_value, expected_value) in enumerate(zip(actual, expected)):
            self.assertTrue(math.isfinite(actual_value), "non-finite value at index %d" % index)
            self.assertLessEqual(
                abs(actual_value - expected_value),
                tolerance,
                "value mismatch at index %d" % index,
            )

    def test_periodic_windows_match_shared_vectors(self):
        vectors = self.vectors["windows"]
        tolerance = vectors["absoluteTolerance"]
        self.assert_sequence_close(
            window_coefficients(8, FFTWindow.HANN),
            vectors["periodicHann8"],
            tolerance,
        )
        self.assert_sequence_close(
            window_coefficients(8, FFTWindow.BLACKMAN),
            vectors["periodicBlackman8"],
            tolerance,
        )

    def test_radix2_fft_matches_shared_vectors(self):
        for vector in self.vectors["fft"]:
            samples = tuple(
                complex(real, imaginary)
                for real, imaginary in zip(vector["realInput"], vector["imaginaryInput"])
            )
            actual = radix2_fft(samples)
            tolerance = vector["absoluteTolerance"]
            self.assert_sequence_close(
                tuple(value.real for value in actual),
                vector["realOutput"],
                tolerance,
            )
            self.assert_sequence_close(
                tuple(value.imag for value in actual),
                vector["imaginaryOutput"],
                tolerance,
            )


if __name__ == "__main__":
    unittest.main()
