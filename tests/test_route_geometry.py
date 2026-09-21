"""Unit tests for services/route_geometry.py — point-to-route distance."""
from __future__ import annotations

import math

import pytest

from app.services.route_geometry import distance_to_shapes_m

# One degree of latitude is ~111.2 km, so 0.001° ≈ 111 m.
_LAT, _LON = 39.7392, -104.9903


def test_no_shapes_is_infinitely_far():
    assert distance_to_shapes_m(_LAT, _LON, []) == math.inf
    assert distance_to_shapes_m(_LAT, _LON, [[]]) == math.inf


def test_point_on_the_line_is_zero():
    line = [[_LAT, _LON - 0.01], [_LAT, _LON + 0.01]]
    assert distance_to_shapes_m(_LAT, _LON, [line]) == pytest.approx(0.0, abs=1e-6)


def test_measures_to_the_line_not_the_nearest_vertex():
    # Vertices are ~1.7 km either side; the vehicle sits 111 m off the middle
    # of the straight run between them.
    line = [[_LAT + 0.001, _LON - 0.02], [_LAT + 0.001, _LON + 0.02]]
    assert distance_to_shapes_m(_LAT, _LON, [line]) == pytest.approx(111.2, abs=0.5)


def test_beyond_the_end_of_the_line_measures_to_the_endpoint():
    # Segment ends 0.001° east of the point; distance is to that endpoint.
    line = [[_LAT, _LON + 0.001], [_LAT, _LON + 0.01]]
    expected = 0.001 * 111_195 * math.cos(math.radians(_LAT))
    assert distance_to_shapes_m(_LAT, _LON, [line]) == pytest.approx(expected, rel=1e-3)


def test_takes_the_nearest_of_several_shapes():
    far = [[_LAT + 0.01, _LON - 0.01], [_LAT + 0.01, _LON + 0.01]]
    near = [[_LAT - 0.0005, _LON - 0.01], [_LAT - 0.0005, _LON + 0.01]]
    assert distance_to_shapes_m(_LAT, _LON, [far, near]) == pytest.approx(55.6, abs=0.5)


def test_single_point_shape_and_degenerate_segment():
    assert distance_to_shapes_m(_LAT, _LON, [[[_LAT + 0.001, _LON]]]) == pytest.approx(111.2, abs=0.5)
    dup = [[_LAT + 0.001, _LON], [_LAT + 0.001, _LON]]
    assert distance_to_shapes_m(_LAT, _LON, [dup]) == pytest.approx(111.2, abs=0.5)
