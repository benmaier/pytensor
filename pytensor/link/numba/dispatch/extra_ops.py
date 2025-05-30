import warnings
from typing import cast

import numba
import numpy as np

from pytensor.graph import Apply
from pytensor.link.numba.dispatch import basic as numba_basic
from pytensor.link.numba.dispatch.basic import get_numba_type, numba_funcify
from pytensor.raise_op import CheckAndRaise
from pytensor.tensor import TensorVariable
from pytensor.tensor.extra_ops import (
    Bartlett,
    CumOp,
    FillDiagonal,
    FillDiagonalOffset,
    RavelMultiIndex,
    Repeat,
    SearchsortedOp,
    Unique,
    UnravelIndex,
)


@numba_funcify.register(Bartlett)
def numba_funcify_Bartlett(op, **kwargs):
    @numba_basic.numba_njit(inline="always")
    def bartlett(x):
        return np.bartlett(numba_basic.to_scalar(x))

    return bartlett


@numba_funcify.register(CumOp)
def numba_funcify_CumOp(op: CumOp, node: Apply, **kwargs):
    axis = op.axis
    mode = op.mode
    ndim = cast(TensorVariable, node.outputs[0]).ndim

    if axis is not None:
        if axis < 0:
            axis = ndim + axis
        if axis < 0 or axis >= ndim:
            raise ValueError(f"Invalid axis {axis} for array with ndim {ndim}")

        reaxis_first = (axis, *(i for i in range(ndim) if i != axis))
        reaxis_first_inv = tuple(np.argsort(reaxis_first))

    if mode == "add":
        if axis is None or ndim == 1:

            @numba_basic.numba_njit
            def cumop(x):
                return np.cumsum(x)

        else:

            @numba_basic.numba_njit(boundscheck=False)
            def cumop(x):
                out_dtype = x.dtype
                if x.shape[axis] < 2:
                    return x.astype(out_dtype)

                x_axis_first = x.transpose(reaxis_first)
                res = np.empty(x_axis_first.shape, dtype=out_dtype)

                res[0] = x_axis_first[0]
                for m in range(1, x.shape[axis]):
                    res[m] = res[m - 1] + x_axis_first[m]

                return res.transpose(reaxis_first_inv)

    else:
        if axis is None or ndim == 1:

            @numba_basic.numba_njit
            def cumop(x):
                return np.cumprod(x)

        else:

            @numba_basic.numba_njit(boundscheck=False)
            def cumop(x):
                out_dtype = x.dtype
                if x.shape[axis] < 2:
                    return x.astype(out_dtype)

                x_axis_first = x.transpose(reaxis_first)
                res = np.empty(x_axis_first.shape, dtype=out_dtype)

                res[0] = x_axis_first[0]
                for m in range(1, x.shape[axis]):
                    res[m] = res[m - 1] * x_axis_first[m]

                return res.transpose(reaxis_first)

    return cumop


@numba_funcify.register(FillDiagonal)
def numba_funcify_FillDiagonal(op, **kwargs):
    @numba_basic.numba_njit
    def filldiagonal(a, val):
        np.fill_diagonal(a, val)
        return a

    return filldiagonal


@numba_funcify.register(FillDiagonalOffset)
def numba_funcify_FillDiagonalOffset(op, node, **kwargs):
    @numba_basic.numba_njit
    def filldiagonaloffset(a, val, offset):
        height, width = a.shape

        if offset >= 0:
            start = numba_basic.to_scalar(offset)
            num_of_step = min(min(width, height), width - offset)
        else:
            start = -numba_basic.to_scalar(offset) * a.shape[1]
            num_of_step = min(min(width, height), height + offset)

        step = a.shape[1] + 1
        end = start + step * num_of_step
        b = a.ravel()
        b[start:end:step] = val
        # TODO: This isn't implemented in Numba
        # a.flat[start:end:step] = val
        # return a
        return b.reshape(a.shape)

    return filldiagonaloffset


@numba_funcify.register(RavelMultiIndex)
def numba_funcify_RavelMultiIndex(op, node, **kwargs):
    mode = op.mode
    order = op.order

    if order != "C":
        raise NotImplementedError(
            "Numba does not implement `order` in `numpy.ravel_multi_index`"
        )

    if mode == "raise":

        @numba_basic.numba_njit
        def mode_fn(*args):
            raise ValueError("invalid entry in coordinates array")

    elif mode == "wrap":

        @numba_basic.numba_njit(inline="always")
        def mode_fn(new_arr, i, j, v, d):
            new_arr[i, j] = v % d

    elif mode == "clip":

        @numba_basic.numba_njit(inline="always")
        def mode_fn(new_arr, i, j, v, d):
            new_arr[i, j] = min(max(v, 0), d - 1)

    if node.inputs[0].ndim == 0:

        @numba_basic.numba_njit
        def ravelmultiindex(*inp):
            shape = inp[-1]
            arr = np.stack(inp[:-1])

            new_arr = arr.T.astype(np.float64).copy()
            for i, b in enumerate(new_arr):
                if b < 0 or b >= shape[i]:
                    mode_fn(new_arr, i, 0, b, shape[i])

            a = np.ones(len(shape), dtype=np.float64)
            a[: len(shape) - 1] = np.cumprod(shape[-1:0:-1])[::-1]
            return np.array(a.dot(new_arr.T), dtype=np.int64)

    else:

        @numba_basic.numba_njit
        def ravelmultiindex(*inp):
            shape = inp[-1]
            arr = np.stack(inp[:-1])

            new_arr = arr.T.astype(np.float64).copy()
            for i, b in enumerate(new_arr):
                # no strict argument to this zip because numba doesn't support it
                for j, (d, v) in enumerate(zip(shape, b)):
                    if v < 0 or v >= d:
                        mode_fn(new_arr, i, j, v, d)

            a = np.ones(len(shape), dtype=np.float64)
            a[: len(shape) - 1] = np.cumprod(shape[-1:0:-1])[::-1]
            return a.dot(new_arr.T).astype(np.int64)

    return ravelmultiindex


@numba_funcify.register(Repeat)
def numba_funcify_Repeat(op, node, **kwargs):
    axis = op.axis

    use_python = False

    if axis is not None:
        use_python = True

    if use_python:
        warnings.warn(
            (
                "Numba will use object mode to allow the "
                "`axis` argument to `numpy.repeat`."
            ),
            UserWarning,
        )

        ret_sig = get_numba_type(node.outputs[0].type)

        @numba_basic.numba_njit
        def repeatop(x, repeats):
            with numba.objmode(ret=ret_sig):
                ret = np.repeat(x, repeats, axis)
            return ret

    else:
        repeats_ndim = node.inputs[1].ndim

        if repeats_ndim == 0:

            @numba_basic.numba_njit(inline="always")
            def repeatop(x, repeats):
                return np.repeat(x, repeats.item())

        else:

            @numba_basic.numba_njit(inline="always")
            def repeatop(x, repeats):
                return np.repeat(x, repeats)

    return repeatop


@numba_funcify.register(Unique)
def numba_funcify_Unique(op, node, **kwargs):
    axis = op.axis

    use_python = False

    if axis is not None:
        use_python = True

    return_index = op.return_index
    return_inverse = op.return_inverse
    return_counts = op.return_counts

    returns_multi = return_index or return_inverse or return_counts
    use_python |= returns_multi

    if not use_python:

        @numba_basic.numba_njit(inline="always")
        def unique(x):
            return np.unique(x)

    else:
        warnings.warn(
            (
                "Numba will use object mode to allow the "
                "`axis` and/or `return_*` arguments to `numpy.unique`."
            ),
            UserWarning,
        )

        if returns_multi:
            ret_sig = numba.types.Tuple([get_numba_type(o.type) for o in node.outputs])
        else:
            ret_sig = get_numba_type(node.outputs[0].type)

        @numba_basic.numba_njit
        def unique(x):
            with numba.objmode(ret=ret_sig):
                ret = np.unique(x, return_index, return_inverse, return_counts, axis)
            return ret

    return unique


@numba_funcify.register(UnravelIndex)
def numba_funcify_UnravelIndex(op, node, **kwargs):
    order = op.order

    if order != "C":
        raise NotImplementedError(
            "Numba does not support the `order` argument in `numpy.unravel_index`"
        )

    if len(node.outputs) == 1:

        @numba_basic.numba_njit(inline="always")
        def maybe_expand_dim(arr):
            return arr

    else:

        @numba_basic.numba_njit(inline="always")
        def maybe_expand_dim(arr):
            return np.expand_dims(arr, 1)

    @numba_basic.numba_njit
    def unravelindex(arr, shape):
        a = np.ones(len(shape), dtype=np.int64)
        a[1:] = shape[:0:-1]
        a = np.cumprod(a)[::-1]

        # PyTensor actually returns a `tuple` of these values, instead of an
        # `ndarray`; however, this `ndarray` result should be able to be
        # unpacked into a `tuple`, so this discrepancy shouldn't really matter
        return ((maybe_expand_dim(arr) // a) % shape).T

    return unravelindex


@numba_funcify.register(SearchsortedOp)
def numba_funcify_Searchsorted(op, node, **kwargs):
    side = op.side

    use_python = False
    if len(node.inputs) == 3:
        use_python = True

    if use_python:
        warnings.warn(
            (
                "Numba will use object mode to allow the "
                "`sorter` argument to `numpy.searchsorted`."
            ),
            UserWarning,
        )

        ret_sig = get_numba_type(node.outputs[0].type)

        @numba_basic.numba_njit
        def searchsorted(a, v, sorter):
            with numba.objmode(ret=ret_sig):
                ret = np.searchsorted(a, v, side, sorter)
            return ret

    else:

        @numba_basic.numba_njit(inline="always")
        def searchsorted(a, v):
            return np.searchsorted(a, v, side)

    return searchsorted


@numba_funcify.register(CheckAndRaise)
def numba_funcify_CheckAndRaise(op, node, **kwargs):
    error = op.exc_type
    msg = op.msg

    @numba_basic.numba_njit
    def check_and_raise(x, *conditions):
        for cond in conditions:
            if not cond:
                raise error(msg)
        return x

    return check_and_raise
