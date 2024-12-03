"""UDFs for hologram reconstruction.

Based on the functions available in :code:`libertem_holo.base.reconstr`.
"""
from __future__ import annotations

from typing import Any

import numpy as np
from libertem.udf import UDF

from libertem_holo.base.mask import disk_aperture
from libertem_holo.base.reconstr import get_slice_fft, reconstruct_frame


class HoloReconstructUDF(UDF):
    """Reconstruct off-axis electron holograms using a Fourier-based method.

    Running :meth:`~libertem.api.Context.run_udf` on an instance of this class
    will reconstruct a complex electron wave. Use the :code:`wave` key to access
    the raw data in the result.

    See :ref:`holography app` for detailed application example

    .. versionadded:: 0.3.0

    Examples
    --------
    >>> assert False
    >>> shape = tuple(dataset.shape.sig)
    >>> sb_position = [2, 3]
    >>> sb_size = 4.4
    >>> holo_udf = HoloReconstructUDF(out_shape=shape,
    ...                               sb_position=sb_position,
    ...                               sb_size=sb_size)
    >>> wave = ctx.run_udf(dataset=dataset, udf=holo_udf)['wave'].data

    """

    def __init__(
        self,
        *,
        out_shape: tuple[int, int],
        sb_position: tuple[float, float],
        aperture: np.ndarray,
        precision: bool = True,
    ) -> None:
        """Off-axis electron holography reconstruction.

        Parameters
        ----------
        out_shape
            Shape of the returned complex wave image. Note that the result
            should fit into the main memory.
            See :ref:`holography app` for more details

        sb_position
            Coordinates of sideband position with respect to non-shifted FFT of
            a hologram

        precision
            Defines precision of the reconstruction, True for complex128 for the
            resulting complex wave, otherwise results will be complex64

        aperture
            The aperture used to mask out the sideband. Should have
            a shape equal to the `out_shape` parameter.

        """
        super().__init__(
            out_shape=out_shape,
            sb_position=sb_position,
            precision=precision,
            aperture=aperture,
        )

    def get_result_buffers(self) -> dict[str, Any]:
        ""
        extra_shape = self.params.out_shape
        dtype = np.complex128 if self.params.precision else np.complex64
        return {
            "wave": self.buffer(kind="nav", dtype=dtype, extra_shape=extra_shape),
        }

    def get_task_data(self) -> dict[str, Any]:
        ""
        slice_fft = get_slice_fft(
            self.params.out_shape,
            self.meta.partition_shape.sig,
        )

        return {
            "aperture": self.xp.array(self.params.aperture),
            "slice": slice_fft,
        }

    def process_frame(self, frame: np.ndarray) -> None:
        ""
        wav = reconstruct_frame(
            frame,
            sb_pos=self.params.sb_position,
            aperture=self.task_data.aperture,
            slice_fft=self.task_data.slice,
            precision=self.params.precision,
            xp=self.xp,
        )

        self.results.wave[:] = self.forbuf(wav, self.results.wave)

    def get_backends(self) -> tuple[str, ...]:
        ""
        return ("numpy", "cupy")

    @classmethod
    def with_default_aperture(
        cls,
        *,
        out_shape: tuple[int, int],
        sb_size: float,
        sb_position: tuple[float, float],
        precision: bool = True,
    ) -> HoloReconstructUDF:
        """Instantiate with a default disk-shaped aperture.

        Examples
        --------

        >>> udf = HoloReconstructUDF.with_default_aperture(
        ...     out_shape=(128, 128),
        ...     sb_size=7.6,
        ...     sb_position=(32, 32),
        ... )
        """
        aperture = disk_aperture(out_shape=out_shape, radius=sb_size)
        return cls(
            out_shape=out_shape,
            sb_position=sb_position,
            aperture=aperture,
            precision=precision,
        )
