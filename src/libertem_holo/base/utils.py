"""Utility functions for working with holography data."""

# Functions freq_array, aperture_function, estimate_sideband_position
# estimate_sideband_size are adopted from Hyperspy
# and are subject of following copyright:
#
#  Copyright 2007-2016 The HyperSpy developers
#
#  HyperSpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
#  HyperSpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with  HyperSpy.  If not, see <http://www.gnu.org/licenses/>.
#
# Copyright 2019 The LiberTEM developers
#
#  LiberTEM is distributed under the terms of the GNU General
# Public License as published by the Free Software Foundation,
# version 3 of the License.
# see: https://github.com/LiberTEM/LiberTEM

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal
import typing
import logging

try:
    import cupy as cp
except ImportError:
    cp = None
import numpy as np
from numpy.fft import fft2
from skimage.draw import polygon
from scipy.ndimage import gaussian_filter
from sparseconverter import NUMPY, for_backend


log = logging.getLogger(__name__)

XPType = Any  # Union[Module("numpy"), Module("cupy")]


def freq_array(
    shape: tuple[int, int],
    sampling: tuple[float, float] = (1.0, 1.0),
) -> np.ndarray:
    """Generate a frequency array.

    Parameters
    ----------
    shape : (int, int)
        The shape of the array.
    sampling: (float, float), optional, (Default: (1., 1.))
        The sampling rates of the array.

    Returns
    -------
        Array of the frequencies.

    """
    f_freq_1d_y = np.fft.fftfreq(shape[0], sampling[0])
    f_freq_1d_x = np.fft.fftfreq(shape[1], sampling[1])
    f_freq_mesh = np.meshgrid(f_freq_1d_x, f_freq_1d_y)
    return np.hypot(f_freq_mesh[0], f_freq_mesh[1])


def get_slice_fft(
    out_shape: tuple[int, int],
    sig_shape: tuple[int, int],
) -> tuple[slice, slice]:
    """Get a slice in fourier space to achieve the given output shape."""
    sy, sx = sig_shape
    oy, ox = out_shape

    y_min = int(sy / 2 - oy / 2)
    y_max = int(sy / 2 + oy / 2)
    x_min = int(sx / 2 - ox / 2)
    x_max = int(sx / 2 + ox / 2)
    return (slice(y_min, y_max), slice(x_min, x_max))


def estimate_sideband_position(
    holo_data: np.ndarray,
    holo_sampling: tuple[float, float],
    central_band_mask_radius: float | None = None,
    sb: Literal["lower", "upper"] = "lower",
    xp: XPType = np,
) -> tuple[float, float]:
    """Find the position of the sideband and return its position.

    Parameters
    ----------
    holo_data: ndarray
        The data of the hologram.
    holo_sampling: tuple
        The sampling rate in both image directions.
    central_band_mask_radius: float, optional
        The aperture radius used to mask out the centerband.
    sb : str, optional
        Chooses which sideband is taken. 'lower' or 'upper'
    xp
        Pass in either the numpy or cupy module to select CPU or GPU processing

    Returns
    -------
    Tuple of the sideband position (y, x), referred to the unshifted FFT.

    """
    from .filters import disk_aperture
    sb_position = (0, 0)

    f_freq = freq_array(holo_data.shape, holo_sampling)

    # If aperture radius of centerband is not given, it will be set to 5 % of
    # the Nyquist frequency.
    if central_band_mask_radius is None:
        central_band_mask_radius = 1 / 20.0 * np.max(f_freq)

    aperture = disk_aperture(
        holo_data.shape,
        central_band_mask_radius,
        xp=xp,
    )

    # A small aperture masking out the centerband.
    aperture_central_band = np.subtract(1.0, aperture)
    # imitates 0

    fft_holo = fft2(holo_data) / np.prod(holo_data.shape)
    fft_filtered = fft_holo * aperture_central_band

    # Sideband position in pixels referred to unshifted FFT
    if sb == "lower":
        fft_sb = fft_filtered[: int(fft_filtered.shape[0] / 2), :]
        sb_position = (
            np.unravel_index(np.abs(fft_sb).argmax(), fft_sb.shape)
        )
    elif sb == "upper":
        fft_sb = fft_filtered[int(fft_filtered.shape[0] / 2):, :]
        sb_position = np.unravel_index(np.abs(fft_sb).argmax(), fft_sb.shape)
        sb_position = (
            xp.add(
                xp.asarray(sb_position),
                xp.asarray([int(fft_filtered.shape[0] / 2), 0]),
            )
        )
        if xp is cp:
            sb_position = sb_position.get()

    return tuple(float(c) for c in sb_position)


def estimate_sideband_size(
    sb_position: tuple[float, float],
    holo_shape: tuple[int, int],
    sb_size_ratio: float = 0.5,
    xp: XPType = np,
) -> float:
    """Estimate the size of sideband filter.

    Parameters
    ----------
    holo_shape : array_like
            Holographic data array
    sb_position : tuple
        The sideband position (y, x), referred to the non-shifted FFT.
    sb_size_ratio : float, optional
        Size of sideband as a fraction of the distance to central band
    xp
        Pass in either the numpy or cupy module to select CPU or GPU processing

    Returns
    -------
    sb_size : float
        Size of sideband filter

    """
    h = (
        xp.array(
            (
                xp.asarray(sb_position) - xp.asarray([0, 0]),
                xp.asarray(sb_position) - xp.asarray([0, holo_shape[1]]),
                xp.asarray(sb_position) - xp.asarray([holo_shape[0], 0]),
                xp.asarray(sb_position) - xp.asarray(holo_shape),
            ),
        )
        * sb_size_ratio
    )
    return xp.min(xp.linalg.norm(h, axis=1))


class HoloParams(typing.NamedTuple):
    """Holoparams class contians all parameters necessary for reconstruction."""

    sb_size: tuple[float, float]
    sb_position: tuple[float, float]
    aperture: np.ndarray  # actually can be a cupy ndarray, too! hm...
    orig_shape: tuple[int, int]
    out_shape: tuple[int, int]
    scale_factor: float  # by how much is the phase image scaled down
    xp: XPType

    @property
    def sb_position_int(self) -> tuple[int, int]:
        """Sideband position from float to int."""
        return tuple(
            int(c)
            for c in self.sb_position
        )

    @classmethod
    def from_hologram(
        cls,
        hologram: np.ndarray,
        *,
        central_band_mask_radius: int,
        out_shape: tuple = None,
        line_filter_length: float = 0.9,
        line_filter_width: float = 20,
        xp: XPType = np,
    ) -> HoloParams:
        """Return reconstruction parameters."""
        from .filters import butterworth_line, butterworth_disk
        hologram = xp.asarray(hologram)

        sb_position = estimate_sideband_position(
            holo_data=hologram,
            holo_sampling=(1, 1),
            sb='upper',
            central_band_mask_radius=central_band_mask_radius,
            xp=xp,
        )
        sb_size = estimate_sideband_size(sb_position, hologram.shape, xp=xp)

        if out_shape is None:
            out_side = 2 * int(sb_size) + 16
            out_shape = (out_side, out_side)

        fft_slice = get_slice_fft(out_shape, hologram.shape)

        #  Disk aperture
        aperture = butterworth_disk(hologram.shape, radius=sb_size, order=20)

        sb_position_int = tuple(
            int(c)
            for c in sb_position
        )
        lf = butterworth_line(
            shape=hologram.shape,
            width=line_filter_width,
            sb_position=fft_shift_coords(
                sb_position_int, shape=hologram.shape
            ),
            length_ratio=line_filter_length,
            order=2,
        )
        aperture = np.fft.fftshift(aperture[fft_slice] * lf[fft_slice])
        aperture = xp.asarray(aperture)

        return cls(
            sb_size=sb_size,
            sb_position=sb_position,
            aperture=aperture,
            out_shape=out_shape,
            orig_shape=hologram.shape,
            scale_factor=out_shape[0] / hologram.shape[0],
            xp=xp,
        )

    def filter_aperture_gaussian(self, sigma: float) -> HoloParams:
        aperture = for_backend(self.aperture, NUMPY)
        new_aperture = self.xp.asarray(gaussian_filter(aperture, sigma=sigma))
        return HoloParams(
            sb_size=self.sb_size,
            sb_position=self.sb_position,
            aperture=new_aperture,
            orig_shape=self.orig_shape,
            out_shape=self.out_shape,
            scale_factor=self.scale_factor,
            xp=self.xp,
        )


@lru_cache
def shifted_coords_for_shape(shape):
    return np.fft.fftshift(np.moveaxis(np.mgrid[0:shape[0], 0:shape[1]], 0, -1), axes=(0, 1))


def fft_shift_coords(pos, shape):
    coords = shifted_coords_for_shape(shape)
    return tuple(int(n) for n in coords[pos[0], pos[1]])


def other_sb(sb_position, shape):
    """
    Given the sb_position (as from the estimate function in fft coordinates),
    and the shape of the hologram, calculate the position of the other sideband
    position (also in fft coordinates)
    """
    sb_pos_shifted = fft_shift_coords(sb_position, shape)
    center = (shape[0]//2, shape[1]//2)
    center_to_sb = (
        sb_pos_shifted[0]-center[0],
        sb_pos_shifted[1]-center[1],
    )
    other_sb = (
        center[0] - center_to_sb[0],
        center[1] - center_to_sb[1],
    )
    return fft_shift_coords(other_sb, shape)


def line_filter_coords(length_ratio, sb_position_shifted, width, orig_shape):
    # let's start from a "unit rectangle" which is 1x1 and not rotated:
    coords = np.array([
        [0, 0],
        [0, 1],
        [1, 1],
        [1, 0],
    ]).astype(np.float32)

    # shift to the origin in y direction:
    coords -= np.array([0.5, 0])

    # let's determine the length from the sideband position and ratio:
    center = (orig_shape[0]//2, orig_shape[1]//2)
    center_to_sb = (
        sb_position_shifted[0]-center[0],
        sb_position_shifted[1]-center[1],
    )
    sb_dist = np.linalg.norm(np.array(center_to_sb))
    length = sb_dist * length_ratio
    length_rest = sb_dist * (1 - length_ratio)

    # stretch such that the width (in x direction) corresponds to the length,
    # and the height (y direction) corresponds to the width of the filter:
    scale = np.array([
        [width, 0],
        [0, length],
    ])

    # angle from -pi to +pi between the "x-axis" and the vector from center to sb:
    angle = np.arctan2(*center_to_sb)

    rotate = np.array([
        [np.cos(-angle), np.sin(-angle)],
        [-np.sin(-angle), np.cos(-angle)],
    ])

    # apply scale:
    coords = coords @ scale

    # move to the right:
    coords += np.array([0, length_rest])

    # rotate:
    coords = coords @ rotate

    coords += np.array(center)

    return coords


def draw_lf_rect(dest, orig_shape, sb_position_shifted, length_ratio, width):
    # we "draw" a rotated rectangle into `dest`, starting at `sb_position_shifted`
    # and ending at `length_ratio` times the vector in the direction to the center of `out_shape`.
    coords = line_filter_coords(
        length_ratio=length_ratio,
        sb_position_shifted=sb_position_shifted,
        width=width,
        orig_shape=orig_shape
    )
    rr, cc = polygon(coords[:, 0], coords[:, 1], shape=dest.shape)
    dest[rr, cc] = True
