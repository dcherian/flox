from functools import partial

import numpy as np

from . import xrdtypes as dtypes
from .xrutils import is_scalar, isnull, notnull


def _prepare_for_flox(group_idx, array):
    """
    Sort the input array once to save time.
    """
    assert array.shape[-1] == group_idx.shape[0]

    issorted = (group_idx[:-1] <= group_idx[1:]).all()
    if issorted:
        ordered_array = array
        perm = slice(None)
    else:
        perm = group_idx.argsort(kind="stable")
        group_idx = group_idx[..., perm]
        ordered_array = array[..., perm]
    return group_idx, ordered_array, perm


def _lerp(a, b, *, t, dtype, out=None):
    """
    COPIED from numpy.

    Compute the linear interpolation weighted by gamma on each point of
    two same shape array.

    a : array_like
        Left bound.
    b : array_like
        Right bound.
    t : array_like
        The interpolation weight.
    """
    if out is None:
        out = np.empty_like(a, dtype=dtype)
    with np.errstate(invalid="ignore"):
        diff_b_a = np.subtract(b, a)
    # asanyarray is a stop-gap until gh-13105
    np.add(a, diff_b_a * t, out=out)
    np.subtract(b, diff_b_a * (1 - t), out=out, where=t >= 0.5)
    return out


def quantile_or_topk(
    array,
    inv_idx,
    *,
    q=None,
    k=None,
    axis,
    skipna,
    group_idx,
    dtype=None,
    out=None,
    fill_value=None,
):
    assert q or k
    assert axis == -1

    inv_idx = np.concatenate((inv_idx, [array.shape[-1]]))

    # The approach for quantiles and topk, both of which are basically grouped partition,
    # here is to use (complex_array.partition) because
    # 1. The full np.lexsort((array, labels), axis=-1) is slow and unnecessary
    # 2. Using record_array.partition(..., order=["labels", "array"]) is incredibly slow.
    # partition will first sort by real part, then by imaginary part, so it is a two element
    # lex-partition. Therefore we set
    #     complex_array = group_idx + 1j * array
    # group_idx is an integer (guaranteed), but array can have NaNs. Now,
    #     1 + 1j*NaN = NaN + 1j * NaN
    # so we must replace all NaNs with the maximum array value in the group so these NaNs
    # get sorted to the end.

    # Replace NaNs with the maximum value for each group.
    # Partly inspired by https://krstn.eu/np.nanpercentile()-there-has-to-be-a-faster-way/
    array_nanmask = isnull(array)
    actual_sizes = np.add.reduceat(~array_nanmask, inv_idx[:-1], axis=axis)
    newshape = (1,) * (array.ndim - 1) + (inv_idx.size - 1,)
    full_sizes = np.reshape(np.diff(inv_idx), newshape)
    nanmask = full_sizes != actual_sizes
    # TODO: Don't know if this array has been copied in _prepare_for_flox.
    #       This is potentially wasteful
    array = np.where(array_nanmask, dtypes.get_neg_infinity(array.dtype, min_for_int=True), array)
    maxes = np.maximum.reduceat(array, inv_idx[:-1], axis=axis)
    replacement = np.repeat(maxes, np.diff(inv_idx), axis=axis)
    array[array_nanmask] = replacement[array_nanmask]

    param = q or k
    if k is not None:
        is_scalar_param = False
        param = np.arange(abs(k))
    else:
        is_scalar_param = is_scalar(q)
        param = np.atleast_1d(param)
    param = np.reshape(param, (param.size,) + (1,) * array.ndim)

    # For topk(.., k=+1 or -1), we always return the singleton dimension.
    idxshape = (param.shape[0],) + array.shape[:-1] + (actual_sizes.shape[-1],)

    if q is not None:
        # This is numpy's method="linear"
        # TODO: could support all the interpolations here
        virtual_index = param * (actual_sizes - 1) + inv_idx[:-1]

        if is_scalar_param:
            virtual_index = virtual_index.squeeze(axis=0)
            idxshape = array.shape[:-1] + (actual_sizes.shape[-1],)

        lo_ = np.floor(virtual_index, casting="unsafe", out=np.empty(virtual_index.shape, dtype=np.int64))
        hi_ = np.ceil(virtual_index, casting="unsafe", out=np.empty(virtual_index.shape, dtype=np.int64))
        kth = np.unique(np.concatenate([lo_.reshape(-1), hi_.reshape(-1)]))

    else:
        virtual_index = inv_idx[:-1] + ((actual_sizes - k) if k > 0 else abs(k) - 1)
        kth = np.unique(virtual_index)
        kth = kth[kth >= 0]
        k_offset = param.reshape((abs(k),) + (1,) * virtual_index.ndim)
        lo_ = k_offset + virtual_index[np.newaxis, ...]

    # partition the complex array in-place
    labels_broadcast = np.broadcast_to(group_idx, array.shape)
    with np.errstate(invalid="ignore"):
        cmplx = labels_broadcast + 1j * (array.view(int) if array.dtype.kind in "Mm" else array)
    cmplx.partition(kth=kth, axis=-1)

    if is_scalar_param:
        a_ = cmplx.imag
    else:
        a_ = np.broadcast_to(cmplx.imag, (param.shape[0],) + array.shape)

    if array.dtype.kind in "Mm":
        a_ = a_.astype(array.dtype)

    loval = np.take_along_axis(a_, np.broadcast_to(lo_, idxshape), axis=axis)
    if q is not None:
        # get bounds, Broadcast to (num quantiles, ..., num labels)
        hival = np.take_along_axis(a_, np.broadcast_to(hi_, idxshape), axis=axis)

        # TODO: could support all the interpolations here
        gamma = np.broadcast_to(virtual_index, idxshape) - lo_
        result = _lerp(loval, hival, t=gamma, out=out, dtype=dtype)
    else:
        result = loval
        # This happens if numel in group < abs(k)
        badmask = lo_ < 0
        if badmask.any():
            result[badmask] = fill_value

    if not skipna and np.any(nanmask):
        result[..., nanmask] = fill_value

    if k is not None:
        result = result.astype(dtype, copy=False)
        if out is not None:
            np.copyto(out, result)
    return result


def _np_grouped_op(
    group_idx,
    array,
    op,
    axis=-1,
    size=None,
    fill_value=None,
    dtype=None,
    out=None,
    **kwargs,
):
    """
    most of this code is from shoyer's gist
    https://gist.github.com/shoyer/f538ac78ae904c936844
    """
    # assumes input is sorted, which I do in core._prepare_for_flox
    aux = group_idx

    flag = np.concatenate((np.array([True], like=array), aux[1:] != aux[:-1]))
    uniques = aux[flag]
    (inv_idx,) = flag.nonzero()

    if size is None:
        size = np.max(uniques) + 1
    if dtype is None:
        dtype = array.dtype

    if out is None:
        q = kwargs.get("q", None)
        k = kwargs.get("k", None)
        if q is None and k is None:
            out = np.full(array.shape[:-1] + (size,), fill_value=fill_value, dtype=dtype)
        else:
            nq = len(np.atleast_1d(q)) if q is not None else abs(k)
            out = np.full((nq,) + array.shape[:-1] + (size,), fill_value=fill_value, dtype=dtype)
            kwargs["group_idx"] = group_idx
            kwargs["fill_value"] = fill_value

    if (len(uniques) == size) and (uniques == np.arange(size, like=array)).all():
        # The previous version of this if condition
        #     ((uniques[1:] - uniques[:-1]) == 1).all():
        # does not work when group_idx is [1, 2] for e.g.
        # This happens during binning
        op(array, inv_idx, axis=axis, dtype=dtype, out=out, **kwargs)
    else:
        out[..., uniques] = op(array, inv_idx, axis=axis, dtype=dtype, **kwargs)

    return out


def _nan_grouped_op(group_idx, array, func, fillna, *args, **kwargs):
    if fillna in [dtypes.INF, dtypes.NINF]:
        fillna = dtypes._get_fill_value(kwargs.get("dtype", None) or array.dtype, fillna)
    result = func(group_idx, np.where(isnull(array), fillna, array), *args, **kwargs)
    # np.nanmax([np.nan, np.nan]) = np.nan
    # To recover this behaviour, we need to search for the fillna value
    # (either np.inf or -np.inf), and replace with NaN
    # Our choice of fillna does the right thing for sum, prod
    if fillna in (np.inf, -np.inf):
        allnangroups = result == fillna
        if allnangroups.any():
            result[allnangroups] = kwargs["fill_value"]
    return result


sum = partial(_np_grouped_op, op=np.add.reduceat)
nansum = partial(_nan_grouped_op, func=sum, fillna=0)
prod = partial(_np_grouped_op, op=np.multiply.reduceat)
nanprod = partial(_nan_grouped_op, func=prod, fillna=1)
max = partial(_np_grouped_op, op=np.maximum.reduceat)
nanmax = partial(_nan_grouped_op, func=max, fillna=dtypes.NINF)
min = partial(_np_grouped_op, op=np.minimum.reduceat)
nanmin = partial(_nan_grouped_op, func=min, fillna=dtypes.INF)
quantile = partial(_np_grouped_op, op=partial(quantile_or_topk, skipna=False))
topk = partial(_np_grouped_op, op=partial(quantile_or_topk, skipna=True))
nanquantile = partial(_np_grouped_op, op=partial(quantile_or_topk, skipna=True))
median = partial(partial(_np_grouped_op, q=0.5), op=partial(quantile_or_topk, skipna=False))
nanmedian = partial(partial(_np_grouped_op, q=0.5), op=partial(quantile_or_topk, skipna=True))
# TODO: all, any


def sum_of_squares(group_idx, array, *, axis=-1, size=None, fill_value=None, dtype=None):
    return sum(
        group_idx,
        array**2,
        axis=axis,
        size=size,
        fill_value=fill_value,
        dtype=dtype,
    )


def nansum_of_squares(group_idx, array, *, axis=-1, size=None, fill_value=None, dtype=None):
    return sum_of_squares(
        group_idx,
        np.where(isnull(array), 0, array),
        size=size,
        fill_value=fill_value,
        axis=axis,
        dtype=dtype,
    )


def nanlen(group_idx, array, *args, **kwargs):
    return sum(group_idx, (notnull(array)).astype(int), *args, **kwargs)


def mean(group_idx, array, *, axis=-1, size=None, fill_value=None, dtype=None):
    if fill_value is None:
        fill_value = 0
    out = sum(group_idx, array, axis=axis, size=size, dtype=dtype, fill_value=fill_value)
    with np.errstate(invalid="ignore", divide="ignore"):
        out /= nanlen(group_idx, array, size=size, axis=axis, fill_value=0)
    return out


def nanmean(group_idx, array, *, axis=-1, size=None, fill_value=None, dtype=None):
    if fill_value is None:
        fill_value = 0
    out = nansum(group_idx, array, size=size, axis=axis, dtype=dtype, fill_value=fill_value)
    with np.errstate(invalid="ignore", divide="ignore"):
        out /= nanlen(group_idx, array, size=size, axis=axis, fill_value=0)
    return out


def ffill(group_idx, array, *, axis, **kwargs):
    group_idx, array, perm = _prepare_for_flox(group_idx, array)
    shape = array.shape
    ndim = array.ndim
    assert axis == (ndim - 1), (axis, ndim - 1)

    flag = np.concatenate((np.array([True], like=array), group_idx[1:] != group_idx[:-1]))
    (group_starts,) = flag.nonzero()

    # https://stackoverflow.com/questions/41190852/most-efficient-way-to-forward-fill-nan-values-in-numpy-array
    mask = isnull(array)
    # modified from the SO answer, just reset the index at the start of every group!
    mask[..., np.asarray(group_starts)] = False

    idx = np.where(mask, 0, np.arange(shape[axis]))
    np.maximum.accumulate(idx, axis=axis, out=idx)
    slc = [
        np.arange(k)[tuple([slice(None) if dim == i else np.newaxis for dim in range(ndim)])]
        for i, k in enumerate(shape)
    ]
    slc[axis] = idx

    invert_perm = slice(None) if isinstance(perm, slice) else np.argsort(perm, kind="stable")
    return array[tuple(slc)][..., invert_perm]
