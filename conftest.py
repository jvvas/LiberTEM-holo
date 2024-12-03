"""Global test configuration.

Use this file to define fixtures to use
in both doctests and regular tests.
"""
import numpy as np
import pytest
from libertem.api import Context
from libertem.executor.inline import InlineJobExecutor


@pytest.fixture
def holo_data():
    from libertem_holo.base.generate import hologram_frame

    # Prepare image parameters and mesh
    ny, nx = (5, 7)
    sy, sx = (64, 64)
    slice_crop = (slice(None),
                  slice(None),
                  slice(sy // 4, sy // 4 * 3),
                  slice(sx // 4, sx // 4 * 3))

    lny = np.arange(ny)
    lnx = np.arange(nx)
    lsy = np.arange(sy)
    lsx = np.arange(sx)

    mny, mnx, msy, msx = np.meshgrid(lny, lnx, lsy, lsx)

    # Prepare phase image
    phase_ref = np.pi * msx * (mnx.max() - mnx) * mny / sx**2 \
        + np.pi * msy * mnx * (mny.max() - mny) / sy**2

    # Generate holograms
    holo = np.zeros_like(phase_ref)
    ref = np.zeros_like(phase_ref)

    for i in range(ny):
        for j in range(nx):
            holo[j, i, :, :] = hologram_frame(np.ones((sy, sx)), phase_ref[j, i, :, :])
            ref[j, i, :, :] = hologram_frame(np.ones((sy, sx)), np.zeros((sy, sx)))

    return holo, ref, phase_ref, slice_crop


@pytest.fixture
def lt_ctx():
    return Context(executor=InlineJobExecutor())

@pytest.fixture(autouse=True)
def auto_ds(doctest_namespace, holo_data):
    from libertem.io.dataset.memory import MemoryDataSet
    holo, ref, phase_ref, slice_crop = holo_data

    dataset_holo = MemoryDataSet(data=holo, num_partitions=2, sig_dims=2)
    dataset_ref = MemoryDataSet(data=ref, num_partitions=1, sig_dims=2)

    doctest_namespace["dataset"] = dataset_holo
    doctest_namespace["dataset_ref"] = dataset_ref


@pytest.fixture(autouse=True)
def auto_ctx(doctest_namespace):
    ctx = Context(executor=InlineJobExecutor())
    doctest_namespace["ctx"] = ctx

