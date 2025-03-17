from __future__ import annotations

import typing
from typing import Literal

try:
    import cupy as cp
except ImportError:
    cp = None
import numpy as np
import numpy.typing as npt
import matplotlib.pyplot as plt
from sparseconverter import NUMPY, for_backend
from scipy.ndimage import gaussian_filter
import logging

from libertem_holo.base.reconstr import get_slice_fft, HoloParams, get_phase, reconstruct_bf
from libertem_holo.base.filters import central_line_filter, disk_aperture

log = logging.getLogger(__name__)


def _upsampled_dft(
    corrspecs: npt.NDArray,
    frequencies: tuple[np.ndarray, np.ndarray],
    upsampled_region_size: int,
    axis_offsets: tuple[float, float],
) -> np.ndarray:
    """
    From https://github.com/LiberTEM/LiberTEM-blobfinder, which is itself
    heavily adapted from skimage.registration._phase_cross_correlation.py
    which is itself based on code by Manuel Guizar released initially under a
    BSD 3-Clause license @ https://www.mathworks.com/matlabcentral/fileexchange/18401

    :meta private:
    """
    im2pi = -1j * 2 * np.pi
    upsampled = corrspecs
    for (ax_freq, ax_offset) in zip(frequencies[::-1], axis_offsets[::-1]):
        kernel = np.linspace(
            -ax_offset,
            (-ax_offset + upsampled_region_size - 1),
            num=int(upsampled_region_size),
        )
        kernel = np.exp(kernel[:, None] * ax_freq * im2pi, dtype=np.complex64)
        # Equivalent to:
        #   data[i, j, k] = kernel[i, :] @ data[j, k].T
        upsampled = np.tensordot(kernel, upsampled, axes=(1, -1))
    return upsampled


def _plot_cross_correlate(*, shifted_corr, pos, plot_title, src, target):
    fig, ax = plt.subplots(3, sharex=True, sharey=True)
    ax[0].imshow(for_backend(shifted_corr, NUMPY))
    ax[0].plot(pos[1], pos[0], 'x', color='red')
    ax[1].imshow(for_backend(src, NUMPY))
    ax[1].plot(pos[1], pos[0], 'x', color='red')
    ax[2].imshow(for_backend(target, NUMPY))
    ax[2].plot(pos[1], pos[0], 'x', color='red')
    fig.suptitle(plot_title)


def cross_correlate(
    src,
    target,
    plot: bool = False,
    plot_title: str = "",
    normalization: Literal['phase'] | None = 'phase',
    upsample_factor=1,
    xp=np,
) -> tuple[np.ndarray, np.ndarray]:
    """Rigid image registration by cross-correlation.

    Supports optional phase normalization. Based on the
    `phase_cross_correlation` function of scikit-image, but with added GPU
    support via cupy, and some debugging facilities built in.

    Parameters
    ==========
    src
        The static image, either a numpy or cupy array
        (if cupy, you should set `xp=cp`, too)

    target
        The moving image, either a numpy or cupy array
        (if cupy, you should set `xp=cp`, too)

    normalization
        'phase' or None, same as for `phase_cross_correlation`

    upsample_factor
        Subpixel scaling factor, same as for `phase_cross_correlation`

    xp
        numpy or cupy
    """
    src = xp.asarray(src)
    target = xp.asarray(target)
    src_freq = xp.fft.fftn(src)
    target_freq = xp.fft.fftn(target)
    image_product = src_freq * target_freq.conj()

    if normalization == 'phase':
        eps = np.finfo(image_product.real.dtype).eps
        image_product /= np.maximum(np.abs(image_product), 100 * eps)
    elif normalization is not None:
        raise ValueError(f"unknown normalization {normalization}")

    cross_correlation = xp.fft.ifftn(image_product)
    shifted_corr = xp.fft.fftshift(np.abs(cross_correlation))

    maxima = xp.unravel_index(
        xp.argmax(shifted_corr),
        shifted_corr.shape
    )
    float_dtype = image_product.real.dtype
    midpoint = xp.array([xp.fix(axis_size / 2) for axis_size in src_freq.shape])
    shift = xp.stack(maxima).astype(float_dtype, copy=False)
    shift -= midpoint

    # estimate sublixel shifts using the upsampled DFT method:
    if upsample_factor > 1:
        frequencies = (
            xp.fft.fftfreq(src.shape[0], upsample_factor),
            xp.fft.fftfreq(src.shape[1], upsample_factor),
        )

        # Initial shift estimate in upsampled grid
        upsample_factor = xp.array(upsample_factor, dtype=float_dtype)
        shift = xp.round(shift * upsample_factor) / upsample_factor
        upsampled_region_size = xp.ceil(upsample_factor * 1.5)
        # Center of output array at dftshift + 1
        dftshift = xp.fix(upsampled_region_size / 2.0)
        # Matrix multiply DFT around the current shift estimate
        sample_region_offset = dftshift - np.round(shift * upsample_factor)
        cross_correlation = _upsampled_dft(
            image_product.conj(),
            frequencies,
            upsampled_region_size,
            sample_region_offset,
        ).conj()
        # Locate maximum and map back to original pixel grid
        maxima = xp.unravel_index(
            xp.argmax(xp.abs(cross_correlation)), cross_correlation.shape
        )

        maxima = xp.stack(maxima).astype(float_dtype, copy=False)
        maxima -= dftshift

        shift += maxima / upsample_factor

    if xp is np:
        shift = tuple(float(x) for x in shift)
    else:
        shift = tuple(float(for_backend(x, NUMPY)) for x in shift)

    # for "backwards compat", return correlation maxima and not shift
    pos = xp.array(shift) + midpoint

    if plot:
        _plot_cross_correlate(
            shifted_corr=shifted_corr,
            pos=pos,
            plot_title=plot_title,
            src=src,
            target=target,
        )

    return pos, shifted_corr


def gradient(image: np.ndarray, scale=1):
    scale = [scale] * image.ndim
    gradients = np.gradient(np.asarray(image), *scale)
    return np.stack(gradients, axis=-1)


def get_grad_angle(image, scale=3):
    """From an image, get the angle of the gradient."""
    grad = gradient(image, scale=scale)
    return np.arctan2(grad[..., 0], grad[..., 1])


def get_grad_xy(image, scale=3):
    """From an image, get the angle of the gradient."""
    grad = gradient(image, scale=scale)
    return (grad[..., 0], grad[..., 1])


def is_left(
    a: np.ndarray,
    b: np.ndarray,
    c: np.ndarray,
) -> np.ndarray:
    """
    Points a and b are points on a line and result is an array of
    True or False if c is left or right of line resp.
    """
    return (b[1] - a[1])*(c[0] - a[0]) - (b[0] - a[0])*(c[1] - a[1]) > 0


class Correlator:
    def prepare_input(
        self,
        img: np.ndarray,
    ) -> typing.Any:
        raise NotImplementedError()

    def correlate(
        self,
        ref_image: typing.Any,
        moving_image: typing.Any,
        plot: bool,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        raise NotImplementedError()


class BiprismDeletionCorrelator(Correlator):
    """
    Cross correlation on low magnification while removing biprism.
    """
    def __init__(
        self,
        mask: np.ndarray,
        upsample_factor: int = 1,
        normalization: Literal['phase'] | None = 'phase',
        xp: typing.Any = np,
    ) -> None:
        self._mask = mask
        self._xp = xp
        self._upsample_factor = upsample_factor
        self._normalization = normalization

    def prepare_input(
        self,
        img: np.ndarray,
    ) -> typing.Any:
        overview = np.zeros_like(img)
        overview[:] = img
        overview[self._mask] = img.mean()
        return overview

    def correlate(
        self,
        ref_image: typing.Any,
        moving_image: typing.Any,
        plot: bool,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        pos, corrmap = cross_correlate(
            ref_image,
            moving_image,
            xp=self._xp,
            plot=plot,
            upsample_factor=self._upsample_factor,
            normalization=self._normalization,
        )
        pos_rel = (
            pos[0] - (moving_image.shape[0]) // 2,
            pos[1] - (moving_image.shape[1]) // 2,
        )
        return pos, pos_rel

    @classmethod
    def plot_get_coords(cls, img, coords_out):
        """
        At low magnification, plot image of area with biprism visible.
        Click on edges of biprism to create the coordinates to mask it out,
        for cross correlation.
        First, click one edge of biprism from left side, then same edge, right side.
        Then, click other edge of biprism from left side, then right side.
        -----1-------2-----
        -----3-------4-----
        """
        fig, ax = plt.subplots(1)
        ax.imshow(img)

        def onclick(event):
            plt.plot(event.xdata, event.ydata, 'ro')
            coords_out.append((event.ydata, event.xdata))
            if len(coords_out) == 4:
                fig.canvas.mpl_disconnect(cid)
        cid = fig.canvas.mpl_connect('button_press_event', onclick)

    @classmethod
    def get_masked(cls, img, coords):
        """Uses coordinates from plot_get_coords to create a mask of biprism."""
        yx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
        mask = is_left(coords[0], coords[1], yx) & ~ is_left(coords[2], coords[3], yx)
        return mask


class BrightFieldCorrelator(Correlator):
    """
    Cross correlation on bright field of hologram.
    """
    def __init__(
        self,
        holoparams: HoloParams,
        upsample_factor: int = 1,
        normalization: Literal['phase'] | None = 'phase',
        xp: typing.Any = np,
    ) -> None:
        self._holoparams = holoparams
        self._xp = xp
        self._normalization = normalization
        self._upsample_factor = upsample_factor

    def prepare_input(
        self,
        img: np.ndarray,
    ) -> typing.Any:
        holoparams = self._holoparams
        line_filter = central_line_filter(
            sb_position=holoparams.sb_position_int,
            out_shape=holoparams.out_shape,
            orig_shape=img.shape,
            length_ratio=0.95,
            width=20
        )
        aperture = disk_aperture(out_shape=holoparams.out_shape, radius=holoparams.sb_size//3)
        slice_fft = get_slice_fft(out_shape=holoparams.out_shape, sig_shape=img.shape)
        line_filter = line_filter[slice_fft]
        aperture[np.fft.fftshift(line_filter)] = 0
        aperture = np.fft.fftshift(gaussian_filter(np.fft.fftshift(aperture), sigma=6))
        holo_bf = np.abs(
            reconstruct_bf(
                frame=img,
                aperture=aperture,
                slice_fft=slice_fft,
                xp=self._xp
            )
        )
        holo_bf = np.gradient(holo_bf)[0]
        return holo_bf

    def correlate(
        self,
        ref_image: typing.Any,
        moving_image: typing.Any,
        plot: bool,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        pos, corrmap = cross_correlate(
            ref_image,
            moving_image,
            xp=self._xp,
            plot=plot,
            upsample_factor=self._upsample_factor,
            normalization=self._normalization,
        )
        pos_rel = (
            pos[0] - (moving_image.shape[0]) // 2,
            pos[1] - (moving_image.shape[1]) // 2,
        )
        return pos, pos_rel


class PhaseImageCorrelator(Correlator):
    """
    Cross correlation on reconstructed phase image.
    """

    def __init__(
        self,
        holoparams: HoloParams,
        upsample_factor: int = 1,
        normalization: Literal['phase'] | None = 'phase',
        xp: typing.Any = np,
    ) -> None:
        self._holoparams = holoparams
        self._xp = xp
        self._normalization = normalization
        self._upsample_factor = upsample_factor

    def prepare_input(
        self,
        img: np.ndarray,
    ) -> typing.Any:
        holoparams = self._holoparams
        phase = get_phase(img, holoparams, xp=self._xp)
        return phase

    def correlate(
        self,
        ref_image: typing.Any,
        moving_image: typing.Any,
        plot: bool,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        pos, corrmap = cross_correlate(
            ref_image,
            moving_image,
            xp=self._xp,
            plot=plot,
            normalization=self._normalization,
            upsample_factor=self._upsample_factor,
        )
        pos_rel = (
            pos[0] - (moving_image.shape[0]) // 2,
            pos[1] - (moving_image.shape[1]) // 2,
        )
        return pos, pos_rel


class GradAngleCorrelator(Correlator):
    """
    Cross correlation on gradient angle of phase image.
    """

    def __init__(
        self,
        holoparams: HoloParams,
        upsample_factor: int = 1,
        normalization: Literal['phase'] | None = 'phase',
        xp: typing.Any = np,
    ) -> None:
        self._holoparams = holoparams
        self._xp = xp
        self._normalization = normalization
        self._upsample_factor = upsample_factor

    def prepare_input(
        self,
        img: np.ndarray,
    ) -> np.ndarray:
        holoparams = self._holoparams
        grad_angle = get_grad_angle(get_phase(img, holoparams, xp=self._xp))
        return grad_angle

    def correlate(
        self,
        ref_image: typing.Any,
        moving_image: typing.Any,
        plot: bool,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        pos, corrmap = cross_correlate(
            ref_image,
            moving_image,
            xp=self._xp,
            plot=plot,
            upsample_factor=self._upsample_factor,
            normalization=self._normalization,
        )
        pos_rel = (
            pos[0] - (moving_image.shape[0]) // 2,
            pos[1] - (moving_image.shape[1]) // 2,
        )
        return pos, pos_rel


class GradXYCorrelator(Correlator):
    """
    Cross correlation on gradient x and Y, correlation maps summed.
    """

    def __init__(
        self,
        holoparams: HoloParams,
        xp: typing.Any = np,
    ) -> None:
        self._holoparams = holoparams
        self._xp = xp

    def prepare_input(
        self,
        img: np.ndarray,
    ) -> typing.Any:
        holoparams = self._holoparams
        (grad_x, grad_y) = get_grad_xy(
            get_phase(img, holoparams, xp=self._xp),
            scale=3,
        )
        # because `gradient` interpolates at the edge, we get a nice
        # vertical artifact that the cross correlation latches onto,
        # so we need to slice the edges away. take care to slice
        # everything the same, so the shapes match, and we need to
        # slice enough such that the interpolated region is removed
        # completely (I think this relates to the `scale` argument
        # above):
        return (grad_x[4:-5, 4:-5], grad_y[4:-5, 4:-5])

    def correlate(
        self,
        ref_image: typing.Any,
        moving_image: typing.Any,
        plot: bool,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        xp = self._xp
        ref_image_x, ref_image_y = ref_image
        moving_image_x, moving_image_y = moving_image
        pos_x, corrmap_x = cross_correlate(
            ref_image_x,
            moving_image_x,
            xp=xp,
            plot=plot,
        )
        pos_y, corrmap_y = cross_correlate(
            ref_image_y,
            moving_image_y,
            xp=xp,
            plot=plot,
        )
        corrmap = corrmap_x + corrmap_y
        pos = xp.unravel_index(xp.argmax(corrmap), corrmap.shape)
        if xp is np:
            pos = tuple(float(x) for x in pos)
        else:
            pos = tuple(float(for_backend(x, NUMPY)) for x in pos)
        pos_rel = (
            pos[0] - (moving_image_y.shape[0]) // 2,
            pos[1] - (moving_image_y.shape[1]) // 2,
        )
        return pos, pos_rel


class NoopCorrelator(Correlator):
    """Do nothing, successfully."""

    def prepare_input(
        self,
        img: np.ndarray,
    ) -> typing.Any:
        return img

    def correlate(
        self,
        ref_image: typing.Any,
        moving_image: typing.Any,
        plot: bool,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        return (0.0, 0.0), (0.0, 0.0)
