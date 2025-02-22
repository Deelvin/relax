# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""ONNX: Open Neural Network Exchange importer for Relax.

This module implemnets the required functionality to read ONNX models
and convert them into equivalent Relax functions. The entry point that encapsulates
this functionality is the function from_onnx.

In order to extend the functionality of the importer, you can add new
operators to the operator registry. The operator registry is a dictionary
that maps operator names to operator converters. The registry is defined
in the _get_converter_map function. To add a new operator, you can define
a new class that inherits from the OnnxOpConverter class and implement
the _impl method.

By default, ONNX defines models in terms of dynamic shapes. The ONNX importer
retains dynamic shapes upon import, and when possible, the compiler attempts to
convert the model to use static shapes at compile time.
If this fails, there may still be dynamic operations in the model.
Not all TVM kernels currently support dynamic shapes, please file an issue on
github.com/apache/tvm/issues if you hit an error with dynamic kernels.
"""
import math
import warnings
from typing import Union, List, Dict, Tuple, Any
import onnx.onnx_ml_pb2

import numpy as _np

import tvm
from tvm import relax, topi, relay
from tvm.target import Target
from tvm.ir import IRModule
from tvm.ir.supply import NameSupply
from tvm.relax import testing, PyExprMutator
from tvm.relay.expr import TupleWrapper, Var, GlobalVar
from tvm.relay.frontend.onnx import OnnxOpConverter as RelayOnnxOpConverter


def get_type(elem_type: Union[str, int]) -> str:
    """Converts onnx integer datatype to numpy datatype"""
    # If a string was passed instead of a tensor type, it does not need
    # conversion and can be returned.
    if isinstance(elem_type, str):
        return elem_type

    try:
        from onnx.mapping import TENSOR_TYPE_TO_NP_TYPE  # pylint: disable=import-outside-toplevel
    except ImportError as exception:
        raise ImportError("Unable to import onnx which is required {}".format(exception))

    return str(TENSOR_TYPE_TO_NP_TYPE[elem_type])


def get_info(info_proto: onnx.onnx_ml_pb2.ValueInfoProto) -> Tuple[str, List, str, List]:
    """Extract the shape from a ValueInfoProto.

    Parameters
    ----------
    info_proto: onnx.onnx_ml_pb2.ValueInfoProto
        The ValueInfoProto to extract the info from.

    Returns
    -------
    Tuple[str, List, str, List]
        The name, shape, type, and shape name of the ValueInfoProto.
    """
    shape = []
    shape_name = []
    for dim in info_proto.type.tensor_type.shape.dim:
        name = dim.dim_param
        value = dim.dim_value
        if value is None or value == 0:
            value = tvm.tir.Var("dyn", "int64")
            shape_name.append(name)
        else:
            shape_name.append(value)
        shape.append(value)

    name = info_proto.name
    if info_proto.type.tensor_type.elem_type:
        dtype = get_type(info_proto.type.tensor_type.elem_type)
    else:
        dtype = None
    return name, shape, dtype, shape_name


def get_numpy(tensor_proto: onnx.onnx_ml_pb2.TensorProto) -> _np.ndarray:
    """Grab data in TensorProto and convert to numpy array."""
    try:
        from onnx.numpy_helper import to_array  # pylint: disable=import-outside-toplevel
    except ImportError as exception:
        raise ImportError("Unable to import onnx which is required {}".format(exception))
    return to_array(tensor_proto)


class onnx_input(list):  # pylint: disable=invalid-name
    """A list that returns None when out-of-bounds indices are accessed."""

    def __getitem__(self, item):
        if isinstance(item, slice):
            if item.stop is None:
                stop = len(self)
            else:
                stop = item.stop
            indices = list(range(stop)[item])
            return [self[i] for i in indices]
        if isinstance(item, int):
            return list(self)[item] if item < len(self) else None
        raise TypeError("list indices must be integers or slices, not %s" % type(item).__name__)


# pylint: disable=invalid-name, len-as-condition, unused-argument, too-many-lines, redefined-builtin
class OnnxOpConverter(object):
    """A helper class for holding the common logic for ONNX op converters.
    Each converter maps to a single ONNX op and defines the equivalent
    functionality using Relax expressions. The converter can define multiple versions
    of the op and the version is selected based on the opset version of the model.
    """

    @classmethod
    def get_converter(cls, opset):
        """Get converter matches given opset.

        Parameters
        ----------
        opset: int
            opset from model.

        Returns
        -------
        converter, which should be `_impl_vx`. Number x is the biggest
            number smaller than or equal to opset belongs to all support versions.
        """
        versions = [int(d.replace("_impl_v", "")) for d in dir(cls) if "_impl_v" in d]
        versions = sorted(versions + [opset])
        version = versions[max([i for i, v in enumerate(versions) if v == opset]) - 1]
        if hasattr(cls, "_impl_v{}".format(version)):
            return getattr(cls, "_impl_v{}".format(version))
        raise NotImplementedError(
            "opset version {} of {} not implemented".format(version, cls.__name__)
        )


class MatMul(OnnxOpConverter):
    """Converts an onnx MatMul node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.matmul(inputs[0], inputs[1])


class Div(OnnxOpConverter):
    """Converts an onnx Div node into an equivalent Relax expression."""

    @classmethod
    def _impl_v14(cls, bb, inputs, attr):
        return relax.op.divide(inputs[0], inputs[1])


class Sigmoid(OnnxOpConverter):
    """Converts an onnx Sigmoid node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.sigmoid(inputs[0])


class Softmax(OnnxOpConverter):
    """Converts an onnx Softmax node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        axis = attr.get("axis", -1)
        return relax.op.nn.softmax(inputs[0], axis=axis)


class Transpose(OnnxOpConverter):
    """Converts an onnx Transpose node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        perm = attr.get("perm", None)
        return bb.emit_te(topi.transpose, inputs[0], axes=perm)


class Unsqueeze(OnnxOpConverter):
    """Converts an onnx Unsqueeze node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        axes = inputs[1]

        if isinstance(axes, relax.Constant):
            constant_axes = list(axes.data.numpy())
            constant_axes = list(map(int, constant_axes))
            constant_axes = sorted(constant_axes)
            for axis in constant_axes:
                data = relax.op.expand_dims(data, axis=axis)
            return data

        raise NotImplementedError("Unsqueeze with dynamic axes is not supported.")


class Concat(OnnxOpConverter):
    """Convert an onnx Concat node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        axis = attr.get("axis", 0)
        return relax.op.concat(inputs, axis=axis)


class Add(OnnxOpConverter):
    """Convert an onnx Add node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.add(inputs[0], inputs[1])


class Mul(OnnxOpConverter):
    """Convert an onnx Mul node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.multiply(inputs[0], inputs[1])


class Cast(OnnxOpConverter):
    """Convert an onnx Cast node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        to_type = get_type(attr["to"])
        return bb.emit_te(topi.cast, inputs[0], to_type)


class Gather(OnnxOpConverter):
    """Convert an onnx Gather node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        # TODO This assumes positive only indices.
        axis = attr.get("axis", 0)
        return bb.emit_te(topi.take, inputs[0], inputs[1], axis)


class Gemm(OnnxOpConverter):
    """Convert an onnx Gemm node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        alpha = attr.get("alpha", None)
        beta = attr.get("beta", None)
        transA = attr.get("transA", False)
        transB = attr.get("transB", False)
        A = inputs[0]
        B = inputs[1]
        C = inputs[2]
        dtype = A.checked_type.dtype

        # Compute Y = alpha * A X B + beta * C

        if alpha is not None:
            A = bb.normalize(relax.op.multiply(A, relax.const(alpha, dtype=dtype)))

        Y = bb.emit_te(topi.matmul, A, B, transA, transB)

        if C is not None:
            if beta is not None:
                C = bb.normalize(relax.op.multiply(C, relax.const(beta, dtype=dtype)))
            Y = relax.op.add(Y, C)

        return Y


class Reshape(OnnxOpConverter):
    """Convert an onnx Reshape node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        # TODO We assume new_shape is a constant, need to enable tensor input to reshape
        # for full support.
        if not isinstance(inputs[1], relax.Constant):
            return inputs[0]
        new_shape = inputs[1].data.numpy().tolist()

        # Convert -1 dims in new_shape into positive equivalent.
        if -1 in new_shape:
            if new_shape.count(-1) != 1:
                raise ValueError("Reshape with multiple -1 is not supported.")

            data_shape = [dim.value for dim in data.struct_info.shape.values]
            total_elements = _np.prod(data_shape)
            new_product = 1
            for dim in new_shape:
                if dim > 0:
                    new_product *= dim

            # Replace -1 with positive equivalent
            for i, dim in enumerate(new_shape):
                if dim == -1:
                    new_shape[i] = int(total_elements / new_product)

        return bb.emit_te(topi.reshape, data, new_shape)


class Gelu(OnnxOpConverter):
    """Operator converter for Gelu from Microsoft onnxruntime contrib opset.

    gelu(x) = 0.5x(1 + erf(x/sqrt(2)))
    """

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        return relax.op.nn.gelu(inputs[0])


class BiasGelu(OnnxOpConverter):
    """Operator converter for BiasGelu from Microsoft onnxruntime contrib opset.

    bias_gelu(x, b) = 0.5(x + b)(1 + erf((x + b)/sqrt(2)))
    """

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        inp = relax.op.add(inputs[0], inputs[1])
        return relax.op.nn.gelu(inp)


class Where(OnnxOpConverter):
    """Convert an onnx Where node into an equivalent Relax expression."""

    @classmethod
    def _impl_v16(cls, bb, inputs, attr):
        return relax.op.where(inputs[0], inputs[1], inputs[2])


class Clip(OnnxOpConverter):
    """Converts an onnx Clip node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        results = inputs[0]
        if inputs[1] is not None:
            results = bb.emit_te(topi.maximum, results, inputs[1])
        if inputs[2] is not None:
            results = bb.emit_te(topi.minimum, results, inputs[2])
        return results


class Equal(OnnxOpConverter):
    """Converts an onnx Equal node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.equal(inputs[0], inputs[1])


class Shape(OnnxOpConverter):
    """Converts an onnx Equal node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.shape_of(inputs[0])


class Not(OnnxOpConverter):
    """Converts an onnx Not node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return bb.emit_te(topi.bitwise_not, inputs[0])


class Tanh(OnnxOpConverter):
    """Converts an onnx Tanh node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.tanh(inputs[0])


class Sqrt(OnnxOpConverter):
    """Converts an onnx Sqrt node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.sqrt(inputs[0])


class Relu(OnnxOpConverter):
    """Converts an onnx Relu node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.nn.relu(inputs[0])


class Pow(OnnxOpConverter):
    """Converts an onnx Pow node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return bb.emit_te(topi.power, inputs[0], inputs[1])


class Conv(OnnxOpConverter):
    """Convert an onnx Conv node into an equivalent Relax expression."""

    @classmethod
    def _impl_v11(cls, bb, inputs, attr):
        conv_out = bb.normalize(
            relax.op.nn.conv2d(
                data=inputs[0],
                weight=inputs[1],
                strides=attr.get("strides", 1),
                padding=attr.get("pads", 0),
                dilation=attr.get("dilation", 1),
                groups=attr.get("group", 1),
                data_layout="NCHW",
                kernel_layout="OIHW",
            )
        )
        if inputs[2] is not None:
            conv_out = relax.op.add(conv_out, inputs[2])

        return conv_out


class Erf(OnnxOpConverter):
    """Converts an onnx Erf node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return bb.emit_te(topi.fast_erf, inputs[0])


class CumSum(OnnxOpConverter):
    """Converts an onnx CumSum node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        if len(inputs) > 1:
            axis = int(inputs[1].data.numpy())
        else:
            axis = None
        if attr.get("reverse", 0) != 0:
            data = bb.emit_te(topi.flip, data, axis=axis if axis else 0)
        data = bb.emit_te(
            topi.cumsum,
            data=data,
            axis=axis,
            exclusive=attr.get("exclusive", None),
        )
        if attr.get("reverse", 0) != 0:
            data = bb.emit_te(topi.flip, data, axis=axis if axis else 0)
        return data


class Squeeze(OnnxOpConverter):
    """Converts an onnx Squeeze node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        axis = inputs[1]
        if axis is not None:
            axis = [int(x) for x in inputs[1].data.numpy()]
        return relax.op.squeeze(inputs[0], axis)


class Constant(OnnxOpConverter):
    """Converts an onnx Constant node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        if "value" not in attr:
            raise ValueError("no value in Constant")
        value = attr.pop("value")
        # Constants may rarely have string types. These are likely exported
        # from other frameworks and not actually used in TVM. We'll just use
        # a zero valued constant for compatibility.
        if isinstance(value, bytes):
            np_value = _np.asarray([0]).astype("int64")
        else:
            np_value = get_numpy(value)
        dtype = np_value.dtype.name
        value = relax.const(np_value, dtype)
        return value


class ConstantOfShape(OnnxOpConverter):
    """Converts an onnx ConstantOfShape node into an equivalent Relax expression."""

    @classmethod
    def _impl_v9(cls, bb, inputs, attr):
        shape = inputs[0]
        shape_ndim = [dim.value for dim in shape.struct_info.shape.values][0]
        value = get_numpy(attr.get("value", 0))
        if isinstance(value, _np.ndarray):
            dtype = str(value.dtype)
        else:
            dtype = "float32"
        # Create a constant for the new value.
        const_value = relax.const(value, dtype)

        # Broadcast the constant to the input shape.
        shape_dataflow_var = bb.emit(
            relax.Call(
                relax.ExternFunc("vm.builtin.tensor_to_shape"),
                [shape],
                sinfo_args=[relax.ShapeStructInfo(ndim=shape_ndim)],
            )
        )
        shape_vars = []
        for i in range(shape_ndim):
            shape_vars.append(tvm.tir.Var("x_%d" % i, "int64"))
        bb.match_cast(shape_dataflow_var, relax.ShapeStructInfo(shape_vars))
        return relax.op.broadcast_to(const_value, relax.ShapeExpr(shape_vars))


class Sub(OnnxOpConverter):
    """Converts an onnx Sub node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.subtract(inputs[0], inputs[1])


class Sin(OnnxOpConverter):
    """Converts an onnx Sin node into an equivalent Relax expression."""

    @classmethod
    def _impl_v7(cls, bb, inputs, attr):
        return relax.op.sin(inputs[0])


class Cos(OnnxOpConverter):
    """Converts an onnx Cos node into an equivalent Relax expression."""

    @classmethod
    def _impl_v7(cls, bb, inputs, attr):
        return relax.op.cos(inputs[0])


class Neg(OnnxOpConverter):
    """Converts an onnx Neg node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.negative(inputs[0])


class Abs(OnnxOpConverter):
    """Converts an onnx Abs node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.abs(inputs[0])


class Min(OnnxOpConverter):
    """Converts an onnx Min node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        # Expand inputs, stack them, then perform minimum over the new axis.
        inputs = [bb.normalize(relax.op.expand_dims(i, axis=0)) for i in inputs]
        stacked_tensor = relax.op.concat(inputs, axis=0)
        return relax.op.min(stacked_tensor, axis=0)


class Max(OnnxOpConverter):
    """Converts an onnx Max node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        # Expand inputs, stack them, then perform maximum over the new axis.
        inputs = [bb.normalize(relax.op.expand_dims(i, axis=0)) for i in inputs]
        stacked_tensor = relax.op.concat(inputs, axis=0)
        return relax.op.max(stacked_tensor, axis=0)


class Log(OnnxOpConverter):
    """Converts an onnx Log node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.log(inputs[0])


class Less(OnnxOpConverter):
    """Converts an onnx Less node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.less(inputs[0], inputs[1])


class LessOrEqual(OnnxOpConverter):
    """Converts an onnx LessOrEqual node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        return relax.op.less_equal(inputs[0], inputs[1])


class Split(OnnxOpConverter):
    """Converts an onnx Split node into an equivalent Relax expression."""

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        splits = attr.get("split", None)
        if splits is not None and len(splits) > 1:
            indices = []
            index = 0
            for i in splits[:-1]:
                index += i
                indices.append(index)
        # When splits isnt specified divide evenly over axis.
        else:
            indices = attr["tvm_custom"]["num_outputs"]
        return bb.emit_te(topi.split, inputs[0], indices, attr.get("axis", 0))

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        splits = inputs[1]
        splits_rank = None
        if splits is not None:
            splits_rank = splits.checked_type.ndim
        if splits is not None and splits_rank > 0:
            if isinstance(splits, relax.Constant):
                splits = splits.data.asnumpy()
                indices = []
                index = 0
                for i in splits[:-1]:
                    index += i
                    indices.append(index)
            else:
                raise ValueError("Dynamic Split not yet supported")
        # When splits isnt specified divide evenly over axis.
        else:
            indices = attr["tvm_custom"]["num_outputs"]
        return bb.emit_te(topi.split, inputs[0], indices, axis=attr.get("axis", 0))


class Slice(OnnxOpConverter):
    """Converts an onnx Splice node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        # TODO (jwfromm) currently only supports constant parameters.
        data = inputs[0]
        starts = inputs[1]
        ends = inputs[2]
        axes = inputs[3]
        steps = inputs[4]
        if not all(
            [
                (isinstance(param, relax.Constant) or param is None)
                for param in [starts, ends, axes, steps]
            ]
        ):
            raise ValueError("Only constant Slice parameters are currently supported.")
        # Convert parameters to constant lists.
        starts = starts.data.numpy().tolist()
        ends = ends.data.numpy().tolist()
        if axes is not None:
            axes = axes.data.numpy().tolist()
        else:
            axes = list(range(len(starts)))
        if steps is not None:
            steps = steps.data.numpy().tolist()
        else:
            steps = [1] * len(axes)
        return bb.emit_te(topi.strided_slice, data, starts, ends, strides=steps, axes=axes)


class Pad(OnnxOpConverter):
    """Converts an onnx Pad node into an equivalent Relax expression."""

    @classmethod
    def _impl_v11(cls, bb, inputs, attr):
        pads = inputs[1]
        if len(inputs) == 3 and inputs[2] is not None:
            constant_value = inputs[2].data.numpy().item()
        else:
            constant_value = 0.0

        if isinstance(pads, relax.Constant):
            pad_before, pad_after = _np.split(pads.data.numpy(), 2)
            pad_before = _np.ndarray.tolist(pad_before)
            pad_after = _np.ndarray.tolist(pad_after)
        else:
            raise ValueError("Dynamic pads are not supported yet.")

        pad_mode = attr.get("mode", b"constant").decode("utf-8")
        if not pad_mode in ["constant", "edge", "reflect"]:
            raise tvm.error.OpAttributeInvalid(
                "Value " + pad_mode + ' in attribute "mode" is invalid for operator Pad.'
            )

        if pad_mode == "constant":
            return bb.emit_te(topi.nn.pad, inputs[0], pad_before, pad_after, constant_value)
        elif pad_mode == "reflect":
            return bb.emit_te(topi.nn.mirror_pad, inputs[0], pad_before, pad_after, "REFLECT")
        else:
            # TODO(gigiblender) Support edge mode.
            raise NotImplementedError("Pad mode {} not implemented".format(pad_mode))


class Tile(OnnxOpConverter):
    """Converts an onnx Tile node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        reps = inputs[1]
        if isinstance(reps, relax.Constant):
            reps = reps.data.numpy().tolist()
        else:
            raise ValueError("Dynamic reps for Tile are supported yet.")
        return bb.emit_te(topi.tile, inputs[0], reps)


class Expand(OnnxOpConverter):
    """Converts an onnx Expand node into an equivalent Relax expression."""

    @classmethod
    def _impl_v13(cls, bb, inputs, attr):
        data = inputs[0]
        shape = inputs[1]
        shape_ndim = [dim.value for dim in shape.struct_info.shape.values][0]
        shape_dataflow_var = bb.emit(
            relax.Call(
                relax.ExternFunc("vm.builtin.tensor_to_shape"),
                [shape],
                sinfo_args=[relax.ShapeStructInfo(ndim=shape_ndim)],
            )
        )

        shape_vars = []
        for i in range(shape_ndim):
            shape_vars.append(tvm.tir.Var("x_%d" % i, "int64"))
        bb.match_cast(shape_dataflow_var, relax.ShapeStructInfo(shape_vars))
        return bb.normalize(relax.op.broadcast_to(data, relax.ShapeExpr(shape_vars)))


class Attention(OnnxOpConverter):
    """Converts an onnx.microsoft Attention node into an equivalent Relax expression."""

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        num_heads = attr["num_heads"]

        assert (
            "qkv_hidden_sizes" not in attr
        ), "different hidden sizes for Q, K, V are not currently supported"
        assert "unidirectional" not in attr, "unidirectional attention not current supported"

        # (batch, seq, in_hidden)
        input_emb = inputs[0]

        # (in_hidden, 3 * out_hidden), where out_hidden = num_heads * head_size
        weight = bb.normalize(inputs[1])

        # (3 * out_hidden,)
        bias = bb.normalize(inputs[2])

        # 1. (    batch,              1,        max_seq, max_seq)
        # 2. (    batch, past_seq + seq,)
        # 3. (    batch,            seq, past_seq + seq,)
        # 4. (    batch,)
        # 5. (2 * batch,)
        # For now, we only support case 2.
        mask_index = bb.normalize(inputs[3])

        # (2, batch, num_heads, past_seq, head_size)
        past = inputs[4]

        # (batch, num_heads, seq, seq)
        extra_add = inputs[5]
        input_emb_shape = [val.value for val in input_emb.struct_info.shape.values]
        (batch_size, seq_len, _) = input_emb_shape

        (out_hidden_x3,) = [val.value for val in bias.struct_info.shape.values]
        assert out_hidden_x3 % 3 == 0, "bias shape should be divisible by 3"
        out_hidden = out_hidden_x3 // 3
        assert (
            out_hidden % num_heads == 0
        ), "output hidden size should be divisible by number of attention heads"
        head_size = out_hidden // num_heads

        assert (
            mask_index is not None
        ), "Attention import currently only supports required mask_index"
        mask_index_shape = [val.value for val in mask_index.struct_info.shape.values]
        assert (
            len(mask_index_shape) == 2
            and mask_index_shape[0] == batch_size
            and mask_index_shape[1] == seq_len
        ), "currently only support (batch_size, sequence_length) mask index"

        assert past is None, "past K, V state is not currently supported"
        assert extra_add is None, "extra add to QxK not currently supported"

        split_1 = bb.emit_te(topi.split, weight, 3, 1)
        # split weight and biases and do the matmuls
        w_Q, w_K, w_V = bb.emit(split_1[0]), bb.emit(split_1[1]), bb.emit(split_1[2])

        split_2 = bb.emit_te(topi.split, bias, 3, 0)
        b_Q, b_K, b_V = bb.emit(split_2[0]), bb.emit(split_2[1]), bb.emit(split_2[2])
        # need to merge batch dimensions since TVM matmul is 2D

        # TODO(@yuchen): check reverse_reshape, a hack here
        input_emb = bb.emit_te(
            topi.reshape, input_emb, (input_emb_shape[0] * input_emb_shape[1], input_emb_shape[2])
        )

        mul = bb.emit_te(topi.nn.matmul, input_emb, w_Q)

        Q = bb.emit_te(topi.add, mul, b_Q)

        mul2 = bb.emit_te(topi.nn.matmul, input_emb, w_K)
        K = bb.emit_te(topi.add, mul2, b_K)

        mul3 = bb.emit_te(topi.nn.matmul, input_emb, w_V)
        V = bb.emit_te(topi.add, mul3, b_V)

        # massage tensors in preparation for batched matmul
        def massage(bb, tensor):
            tensor = bb.emit_te(topi.reshape, tensor, (batch_size, seq_len, num_heads, head_size))

            # (batch_size, num_heads, seq_len, head_size)
            tensor = bb.emit_te(topi.transpose, tensor, [0, 2, 1, 3])
            tensor_shape = [val.value for val in tensor.struct_info.shape.values]

            # (batch_size * num_heads, seq_len, head_size)
            # TODO(@yuchen): check reverse_reshape, hack here
            return bb.emit_te(
                topi.reshape,
                tensor,
                (tensor_shape[0] * tensor_shape[1], tensor_shape[2], tensor_shape[3]),
            )

        Q = massage(bb, Q)
        K = massage(bb, K)
        V = massage(bb, V)

        K_present = bb.emit_te(topi.reshape, K, (batch_size, num_heads, seq_len, head_size))
        V_present = bb.emit_te(topi.reshape, V, (batch_size, num_heads, seq_len, head_size))
        present = bb.emit_te(topi.stack, [K_present, V_present], 0)

        att_scores = bb.emit_te(topi.nn.batch_matmul, Q, K, transpose_a=False, transpose_b=True)
        score_dtype = att_scores.checked_type.dtype
        att_scores = bb.emit_te(
            topi.multiply,
            att_scores,
            relax.const(1 / _np.sqrt(head_size), dtype=att_scores.checked_type.dtype),
        )
        att_scores = bb.emit_te(topi.reshape, att_scores, (batch_size, num_heads, seq_len, seq_len))

        # build the attention mask
        att_mask = bb.emit_te(topi.cast, mask_index, score_dtype)
        att_mask = bb.emit_te(topi.expand_dims, att_mask, 1, num_newaxis=2)
        att_mask = bb.emit_te(topi.subtract, relax.const(1, dtype=score_dtype), att_mask)
        att_mask = bb.emit_te(topi.multiply, att_mask, relax.const(-10000, dtype=score_dtype))

        # apply the mask
        att_scores = bb.emit_te(topi.add, att_scores, att_mask)
        att_scores = bb.emit_te(
            topi.reshape, att_scores, (batch_size * num_heads, seq_len, seq_len)
        )

        att_probs = bb.emit_te(topi.nn.softmax, att_scores, axis=-1)

        output = bb.emit_te(
            topi.nn.batch_matmul, att_probs, V, transpose_a=False, transpose_b=False
        )

        # TODO(@yuchen): check reverse_reshape, hack here
        output_shape = [val.value for val in output.struct_info.shape.values]
        output = bb.emit_te(
            topi.reshape,
            output,
            (
                int(output_shape[0]) // num_heads,
                num_heads,
                int(output_shape[1]),
                int(output_shape[2]),
            ),
        )

        output = bb.emit_te(topi.transpose, output, axes=[0, 2, 1, 3])
        output_shape = [val.value for val in output.struct_info.shape.values]
        output = bb.emit_te(
            topi.reshape, output, (int(output_shape[0]), int(output_shape[1]), out_hidden)
        )
        return relax.Tuple([output, present])


class Identity(OnnxOpConverter):
    """Converts an onnx Identity node into an equivalent Relax expression."""

    @classmethod
    def _impl_v1(cls, bb, inputs, attr):
        return inputs[0]


class Resize(OnnxOpConverter):
    """Converts an onnx Resize node into an equivalent Relax expression."""

    @classmethod
    def _impl_v18(cls, bb, inputs, attr):
        # Extract the many attributes of resize.
        coord_mode = attr.get("coordinate_transformation_mode", b"half_pixel").decode("ascii")
        cubic_coeff_a = attr.get("cubic_coeff_a", -0.75)
        exclude_outside = attr.get("exclude_outside", 0)
        extrapolation_value = attr.get("extrapolation_value", 0.0)
        mode = attr.get("mode", b"nearest").decode("ascii")
        rounding_method = attr.get("nearest_mode", b"round_prefer_floor").decode("ascii")

        # Adapt attributes to fit TVM definition.
        if mode == "nearest":
            mode = "nearest_neighbor"

        # Unpack inputs.
        x = inputs[0]
        roi = inputs[1]
        scales = inputs[2]
        sizes = inputs[3]
        ndims = len(x.struct_info.shape)
        assert ndims == 4, "Only resize2d is currently supported."

        assert (
            scales is None or sizes is None
        ), "Only one of scales and sizes can be provided in Resize."

        # Define relax implementation.
        if roi is not None:
            roi = relax.op.concat(
                [
                    relax.op.strided_slice(roi, axes=[0], begin=[2], end=[ndims]),
                    relax.op.strided_slice(roi, axes=[0], begin=[ndims + 2], end=[2 * ndims]),
                ],
                axis=0,
            )
        else:
            roi = [0.0] * 4

        # Convert scales to sizes if needed.
        if scales is not None:
            assert isinstance(scales, relax.Constant), "Only constant scales currently supported."
            scales = scales.data.numpy()
            sizes_shape = [dim.value for dim in x.struct_info.shape]
            sizes = (sizes_shape * scales)[2:].astype("int64").tolist()
        else:
            assert isinstance(
                sizes, relax.Constant
            ), "Only constant output size currently supported."
            sizes = sizes.data.numpy().astype("int64").tolist()[2:]

        # TODO(jwfromm) relax.image.resize2d runs into some issues with dynamism.
        return bb.emit_te(
            topi.image.resize2d,
            x,
            roi,
            sizes,
            layout="NCHW",
            method=mode,
            coordinate_transformation_mode=coord_mode,
            rounding_method=rounding_method,
            bicubic_alpha=cubic_coeff_a,
            bicubic_exclude=exclude_outside,
            extrapolation_value=extrapolation_value,
        )


class Einsum(OnnxOpConverter):
    """Converts an onnx Einsum node into an equivalent Relax expression."""

    @classmethod
    def _impl_v12(cls, bb, inputs, attr):
        equation = attr["equation"].decode("utf-8")
        return bb.emit_te(topi.einsum, equation, *inputs)


class Range(OnnxOpConverter):
    """Converts an onnx Range node into an equivalent Relax expression."""

    @classmethod
    def _impl_v12(cls, bb, inputs, attr):
        # TODO(jwfromm) Something is wrong with topi.arange, doesnt work with any relax expressions.
        # Unpack inputs. Need to add relax.op.resize
        start = inputs[0]
        assert isinstance(start, relax.Constant), "Constant start required for range."
        start = start.data.numpy().tolist()
        limit = inputs[1]
        assert isinstance(limit, relax.Constant), "Constant limit required for range."
        limit = limit.data.numpy().tolist()
        delta = inputs[2]
        assert isinstance(delta, relax.Constant), "Constant delta required for Range."
        step = delta.data.numpy().tolist()
        return bb.emit_te(topi.arange, start, limit, step)


def _get_convert_map():
    return {
        "MatMul": relay.frontend.onnx.MatMul,
        "Concat": Concat,
        "Add": Add,
        "Mul": Mul,
        "Cast": Cast,
        "Gather": Gather,
        "Gemm": Gemm,
        "Reshape": Reshape,
        "Div": Div,
        "Sigmoid": Sigmoid,
        "Softmax": Softmax,
        "Transpose": Transpose,
        "Unsqueeze": Unsqueeze,
        "Gelu": Gelu,
        "BiasGelu": BiasGelu,
        "Where": Where,
        "Clip": Clip,
        "Equal": Equal,
        "Shape": Shape,
        "Not": Not,
        "Tanh": Tanh,
        "Sqrt": Sqrt,
        "Relu": Relu,
        "Conv": relay.frontend.onnx.Conv,
        "Pow": Pow,
        "Erf": Erf,
        "CumSum": CumSum,
        "Squeeze": Squeeze,
        "Constant": Constant,
        "Sub": Sub,
        "Sin": Sin,
        "Cos": Cos,
        "Neg": Neg,
        "Abs": Abs,
        "Min": Min,
        "Max": Max,
        "Log": Log,
        "Less": Less,
        "LessOrEqual": LessOrEqual,
        "LayerNormalization": relay.frontend.onnx.LayerNormalization,
        "SkipLayerNormalization": relay.frontend.onnx.SkipLayerNormalization,
        "EmbedLayerNormalization": relay.frontend.onnx.EmbedLayerNormalization,
        "InstanceNormalization": relay.frontend.onnx.InstanceNorm,
        # defs/reduction
        "ReduceMax": relay.frontend.onnx.ReduceMax,
        "ReduceMin": relay.frontend.onnx.ReduceMin,
        "ReduceSum": relay.frontend.onnx.ReduceSum,
        "ReduceMean": relay.frontend.onnx.ReduceMean,
        "ReduceProd": relay.frontend.onnx.ReduceProd,
        "ReduceLogSumExp": relay.frontend.onnx.ReduceLogSumExp,
        "ReduceLogSum": relay.frontend.onnx.ReduceLogSum,
        "ReduceSumSquare": relay.frontend.onnx.ReduceSumSquare,
        "ReduceL1": relay.frontend.onnx.ReduceL1,
        "ReduceL2": relay.frontend.onnx.ReduceL2,
        "Expand": Expand,
        "ConstantOfShape": ConstantOfShape,
        "Slice": Slice,
        "Attention": Attention,
        "Pad": Pad,
        "Split": Split,
        "Tile": Tile,
        "BatchNormalization": relay.frontend.onnx.BatchNorm,
        "GlobalAveragePool": relay.frontend.onnx.GlobalAveragePool,
        "Flatten": relay.frontend.onnx.Flatten,
        "MaxPool": relay.frontend.onnx.MaxPool,
        "Identity": Identity,
        "Resize": Resize,
        "Einsum": Einsum,
        "Range": Range,
    }


class ONNXGraphImporter:
    """A helper class for handling Relax expression copying from pb2.GraphProto.
    Definition: https://github.com/onnx/onnx/blob/main/onnx/onnx.proto

    Parameters
    ----------
    shape : dict of str to tuple, optional
        The input shape to the graph
    dtype : str or dict of str to str
        The input types to the graph
    target : tvm.target.Target
        The target device of the compiled functions when using the translator.
    sanitize : bool
        Whether to sanitize the input names to be valid Relax identifiers.
    """

    current = None

    def __init__(
        self,
        shape: Dict[str, List],
        dtype: Union[str, Dict[str, str]],
        target: Target,
        sanitize: bool = True,
    ):
        self._nodes: Dict[str, relax.Expr] = {}
        self._inputs: Dict[str, relax.Var] = {}
        self._num_input: int = 0
        self._shape = shape.copy() if shape else {}
        self._input_names: List[str] = []
        self._dtype = dtype
        self.opset: int = None
        self._target: Union[tvm.target.Target, str] = target
        self._name_supply = NameSupply()
        self._sanitize: bool = sanitize
        self.bb: relax.BlockBuilder = relax.BlockBuilder()  # pylint: disable=invalid-name

    def from_onnx(
        self, graph: onnx.onnx_ml_pb2.ModelProto, opset: int
    ) -> Tuple[IRModule, Dict[str, tvm.nd.array]]:
        """Construct Relax expressions from the ONNX graph.
        Onnx graph is a python protobuf object.

        #TODO (gigiblender): Handle model input name sanitization. This has been a problem
        in the Relay importer in the past and we should be careful to avoid it here.

        Parameters
        ----------
        graph : onnx protobuf object
            The loaded onnx graph
        opset : opset version
        Returns
        -------
        mod : tvm.IRModule
            The returned relax module
        params : dict
            A dict of name: tvm.nd.array pairs, used as pretrained weights
        """
        with self.bb.function("main"):
            with self.bb.dataflow() as df:  # pylint: disable=invalid-name, unused-variable
                self.opset = opset
                self._parse_graph_initializers(graph)
                self._parse_graph_input(graph)
                self._check_for_unsupported_ops(graph)
                self._construct_nodes(graph)

                # now return the outputs
                outputs = [self._nodes[self._parse_value_proto(i)] for i in graph.output]
                outputs = outputs[0] if len(outputs) == 1 else relax.Tuple(outputs)

                # Create a function from our output expression and all input variables.
                param_list = [v for k, v in self._inputs.items() if isinstance(v, relax.Var)]
                output_var = self.bb.emit_output(outputs)
            self.bb.emit_func_output(output_var, params=param_list)
        return self.bb.get()

    def _parse_graph_initializers(self, graph: onnx.onnx_ml_pb2.GraphProto):
        """Parse network inputs to relax, aka parameters."""
        for init_tensor in graph.initializer:
            if not init_tensor.name.strip():
                raise ValueError("Tensor's name is required.")
            array = self._parse_array(init_tensor)
            self._nodes[init_tensor.name] = relax.const(array)

    def _sanitize_name(self, name: str) -> str:
        """Sanitize a name to make it a valid identifier.
        If the name is None, returns a string input_0, input_1, etc.
        If the input is an empty string, returns empty_0, empty_1, etc.
        If the input is a string that does not start with a letter or underscore,
        returns input_<name>. Otherwise, returns an unique input name.

        Parameters
        ----------
        name : str
            The name to sanitize
        Returns
        -------
        new_name : str
        """

        if name == "":
            return self._name_supply.fresh_name("empty_")

        new_name = name.replace(".", "_")
        if not new_name[0].isalpha() and new_name[0] != "_":
            new_name = str(self._name_supply.fresh_name("input_" + new_name))
        else:
            new_name = str(self._name_supply.fresh_name(new_name))

        if new_name != name:
            warnings.warn(("Renaming name %s to %s" % (name, new_name)))
        return new_name

    def _new_var(self, var_name: str, shape: List, dtype: str = "float32"):
        """Creates a new Relax variable."""
        return testing.nn.Parameter(shape=shape, dtype=dtype, name=var_name)

    def _parse_graph_input(self, graph: onnx.onnx_ml_pb2.GraphProto):
        """Parse model inputs to Relax parameters."""
        for i in graph.input:
            # from onnx v0.2, GraphProto.input has type ValueInfoProto,
            #  and the name is 'i.name'
            i_name, i_shape, d_type, i_shape_name = get_info(i)
            if i_name not in self._nodes:
                self._num_input += 1
                self._input_names.append(i_name)
                if i_name in self._shape:
                    i_shape = self._shape[i_name]
                else:
                    if "?" in str(i_shape):
                        warning_msg = (
                            "Input %s has unknown dimension shapes: %s. "
                            "Specifying static values may improve performance"
                            % (i_name, str(i_shape_name))
                        )
                        warnings.warn(warning_msg)
                if isinstance(self._dtype, dict):
                    dtype = self._dtype[i_name] if i_name in self._dtype else d_type
                else:
                    dtype = d_type
                var_name = self._sanitize_name(i_name) if self._sanitize else i_name
                self._nodes[i_name] = self._new_var(var_name, shape=i_shape, dtype=dtype)
            self._inputs[i_name] = self._nodes[i_name]

    def _check_for_unsupported_ops(self, graph: onnx.onnx_ml_pb2.GraphProto):
        convert_map = _get_convert_map()
        unsupported_ops = set()
        for node in graph.node:
            op_name = node.op_type
            if (
                op_name not in convert_map
                and op_name != "Constant"
                # and op_name not in _identity_list
            ):
                unsupported_ops.add(op_name)
        if unsupported_ops:
            msg = "The following operators are not supported for frontend ONNX: "
            msg += ", ".join(unsupported_ops)
            raise tvm.error.OpNotImplemented(msg)

    def _construct_nodes(self, graph: onnx.onnx_ml_pb2.GraphProto):
        """Nodes are stored as directed acyclic graph."""
        for node in graph.node:
            op_name = node.op_type
            attr = self._parse_attr(node.attribute)
            # Create and populate input list.
            inputs = onnx_input()
            for i in node.input:
                if i != "":
                    inputs.append(self._nodes[i])
                else:
                    inputs.append(None)
            i_name = self._parse_value_proto(node)
            outputs = node.output
            attr["tvm_custom"] = {}
            attr["tvm_custom"]["name"] = i_name
            attr["tvm_custom"]["num_outputs"] = len(outputs)

            op = self._convert_operator(op_name, inputs, attr, self.opset)
            # Create struct information for the new operator.
            op = self.bb.normalize(op)

            if not isinstance(op, relax.Tuple):
                if isinstance(op.checked_type, tvm.ir.type.TupleType):
                    # This is a var bound to a tuple. We need to unpack it and create
                    # a new tuple.
                    tuple_items = []
                    for i in range(len(op.checked_type.fields)):
                        tuple_items.append(self.bb.emit(relax.TupleGetItem(op, i)))
                    op = relax.Tuple(tuple_items)
                    outputs_num = len(tuple_items)
                else:
                    outputs_num = 1
            else:
                outputs_num = len(op)
            assert (
                len(outputs) <= outputs_num
            ), "Missing outputs during conversion. Expected {} but Got {} in {}.".format(
                len(outputs), outputs_num, op_name
            )

            if outputs_num == 1:
                self._nodes[outputs[0]] = op
            else:
                for k, i in zip(list(outputs), range(len(outputs))):
                    self._nodes[k] = op[i]

    def _parse_value_proto(self, value_proto: onnx.onnx_ml_pb2.GraphProto):
        """Parse ValueProto or raw str."""
        try:
            name = value_proto.name
        except AttributeError:
            name = value_proto
        return name

    def _parse_array(self, tensor_proto: onnx.onnx_ml_pb2.TensorProto) -> tvm.nd.array:
        np_array = get_numpy(tensor_proto).reshape(tuple(tensor_proto.dims))
        return tvm.nd.array(np_array)

    def _parse_attr(self, attr_proto: onnx.onnx_ml_pb2.AttributeProto) -> Dict[str, Any]:
        """Convert a list of AttributeProto to a dict, with names as keys."""
        attrs = {}
        for a in attr_proto:
            for f in ["f", "i", "s", "g"]:
                if a.HasField(f):
                    attrs[a.name] = getattr(a, f)
            for f in ["floats", "ints", "strings"]:
                if list(getattr(a, f)):
                    assert a.name not in attrs, "Only one type of attr is allowed"
                    attrs[a.name] = tuple(getattr(a, f))
            for f in ["t"]:
                if a.HasField(f):
                    attrs[a.name] = getattr(a, f)
            for f in ["tensors"]:
                if list(getattr(a, f)):
                    assert a.name not in attrs, "Only one type of attr is allowed"
                    attrs[a.name] = tuple(getattr(a, f))
            for f in ["graphs"]:
                if list(getattr(a, f)):
                    raise NotImplementedError("Field {} is not supported in relax.".format(f))
            if a.name not in attrs:
                raise ValueError("Cannot parse attribute: \n{}\n.".format(a))
        return attrs

    def _relay_input_adapter(self, inputs: List[relax.Var]) -> List[relay.Var]:
        """Creates equivalent input Relay vars from the input Relax vars"""
        relay_vars = onnx_input()
        for relax_var in inputs:
            shape_values = []
            # Some inputs may be None to indicate that input isnt used.
            if relax_var is None:
                relay_vars.append(None)
            # Otherwise construct a new relay variable mirroring the relax one.
            else:
                for shape_value in relax_var.struct_info.shape.values:
                    shape_values.append(shape_value)
                if isinstance(relax_var, relax.Constant):
                    relay_vars.append(
                        relay.const(relax_var.data, dtype=relax_var.checked_type.dtype)
                    )
                else:
                    relay_vars.append(
                        relay.var(
                            relax_var.name_hint,
                            shape=shape_values,
                            dtype=relax_var.checked_type.dtype,
                        )
                    )
        return relay_vars

    def _relay_output_adapter(
        self,
        relax_inputs: List[Union[relax.Var, relax.Constant]],
        relay_inputs: List[Union[relay.Var, relay.Constant]],
        relay_output: relay.Expr,
    ) -> relax.Expr:
        """Given the output of a relay op from the Onnx relay frontend,
        calls into the relay to relax translator to obtain the equivalent Relax.
        Then unpacks the IRModule obtained and adds the TIR funcs and the
        associated call_tirs to the block builder in use.

        Parameters
        ----------
        relax_inputs : list(relax.Var, relay.Constant)
                The list of relax vars that are inputs to the relax op.
        relay_inputs : list(relay.Var, relay.Constant)
                The list of relay vars that are inputs to the relay op. This is
                obtianed from the _relay_input_adapter function.
        relay_output : relay.Expr
                The output of the relay op from the Onnx relay frontend.
        Returns
        -------
        output : relax.Expr
                The output of the equivalent relax op.
        """
        if isinstance(relay_output, TupleWrapper):
            relay_output = relay_output.tuple_value

        # Create a Relay function with the body returned by the Relay op.
        relay_var_inputs = [input for input in relay_inputs if isinstance(input, relay.Var)]
        function = relay.Function(relay_var_inputs, relay_output)
        # Save the current in-use block builder. The translator uses its own block builder.
        prev_bb = relax.BlockBuilder._current
        relax.BlockBuilder._current = None
        relax_mod = testing.relay_translator.from_relay(function, self._target)
        # Restore the block builder used by the frontend.
        relax.BlockBuilder._current = prev_bb

        # This dict is used by the Mapper mutator to replace the globar vars
        # in the relax_mod with global_vars registered with the in-use block builder.
        global_var_dict = {}
        for global_var, func in relax_mod.functions.items():
            if global_var.name_hint != "main":
                global_var_dict[global_var] = self.bb.add_func(func, global_var.name_hint)

        # This dict is used by the Mapper mutator to replace the relax vars
        # with the inputs.
        relax_input_dict = {}
        for relax_var in relax_inputs:
            if isinstance(relax_var, relax.Var):
                relax_input_dict[relax_var.name_hint] = relax_var

        @relax.expr_functor.mutator
        class Mapper(PyExprMutator):
            """Mutator to replace the global vars and relax vars in the relax_mod
            with the global vars registered with the in-use block builder and the
            relax vars with the inputs.
            """

            def visit_span(self, span: relax.Span):
                return span

            def visit_var_(self, var_node: Var):  # pylint: disable=arguments-differ
                if var_node.name_hint in relax_input_dict:
                    return relax_input_dict[var_node.name_hint]
                return var_node

            def visit_global_var_(self, gv_node: GlobalVar):  # pylint: disable=arguments-differ
                if gv_node in global_var_dict:
                    return global_var_dict[gv_node]
                return gv_node

        assert (
            len([f for f in relax_mod.functions.values() if isinstance(f, relax.Function)]) == 1
        ), "Expected only one Relax function in the module."
        updated_func = Mapper().visit_expr(relax_mod["main"])

        var_bindings = updated_func.body.blocks[0].bindings
        if isinstance(updated_func.ret_struct_info, relax.TupleStructInfo):
            # Returning a tuple.
            final_binding = var_bindings[-2]
            for binding in var_bindings[:-2]:
                self.bb.emit_normalized(binding)
        else:
            final_binding = var_bindings[-1]
            for binding in var_bindings[:-1]:
                self.bb.emit_normalized(binding)

        return final_binding.value

    def _convert_operator(
        self, op_name: str, inputs: List[relax.Function], attrs: Dict, opset: int
    ) -> relax.Function:
        """Convert ONNX operator into a Relax operator.
        The converter must specify conversions explicitly for incompatible name, and
        apply handlers to operator attributes.

        Parameters
        ----------
        op_name : str
            Operator name, such as Convolution, FullyConnected
        inputs : list of tvm.relax.function.Function
            List of inputs.
        attrs : dict
            Dict of operator attributes
        opset : int
            Opset version
        Returns
        -------
        sym : tvm.relax.function.Function
            Converted relax function
        """
        convert_map = _get_convert_map()
        if op_name in convert_map:
            convert_class = convert_map[op_name]
            op_function = convert_class.get_converter(opset)
            # If the op_function is a subclass of Relay OnnxOpConverter then it is a relay op.
            if issubclass(convert_class, RelayOnnxOpConverter):
                relay_inputs = self._relay_input_adapter(inputs)
                # The op_function might change relay_inputs array. Use a copy of the inputs.
                relay_inputs_copy = onnx_input()
                for relay_input in relay_inputs:
                    relay_inputs_copy.append(relay_input)
                # TODO handle params passing
                relay_output = op_function(relay_inputs_copy, attrs, params=[])
                sym = self._relay_output_adapter(inputs, relay_inputs, relay_output)
            else:
                sym = op_function(self.bb, inputs, attrs)
        else:
            raise NotImplementedError("Operator {} not implemented.".format(op_name))
        return sym


def from_onnx(
    model: onnx.onnx_ml_pb2.GraphProto,
    shape: Dict[str, List] = None,
    dtype: str = "float32",
    opset: int = None,
    target: Union[str, Target] = "llvm",
    sanitize_input_names: bool = True,
) -> Tuple[IRModule, Dict]:
    """Convert a ONNX model into an equivalent Relax Function.
    ONNX graphs are represented as Python Protobuf objects.

    The current implementation assumes that the input model is after ONNX v1.1.0.

    Parameters
    ----------
    model : protobuf object
        ONNX ModelProto after ONNX v1.1.0
    shape : dict of str to tuple, optional
        The input shape to the graph
    dtype : str or dict of str to str
        The input types to the graph
    opset : int, optional
        Override to autodetected opset.
        This can be helpful for some testing.
    target : str or Target, optional
        The compilation target used by the Relay to Relax translator.
    sanitize_input_names : bool, optional
        Whether to sanitize the input names to ensure they are valid Relax identifiers.

    Returns
    -------
    mod : tvm.IRModule
        The relax module for compilation
    params : dict of str to tvm.nd.NDArray
        The parameter dict to be used by relax
    """
    # Error if the model version is below 1.1.0
    if model.ir_version < 3:
        raise ValueError(
            "Model IR version {} not supported. Must be at least after 1.1.0.".format(
                model.ir_version
            )
        )

    try:
        import onnx  # pylint: disable=import-outside-toplevel, redefined-outer-name

        if hasattr(onnx.checker, "check_model"):
            # try use onnx's own model checker before converting any model
            try:
                onnx.checker.check_model(model)
            except Exception as exception:  # pylint: disable=c-extension-no-member, broad-except
                # the checker is a bit violent about errors, so simply print warnings here
                warnings.warn(str(exception))
    except ImportError as error:
        raise ImportError("Unable to import onnx which is required {}".format(error))

    if isinstance(target, str):
        target = Target(target)
    g = ONNXGraphImporter(shape, dtype, target, sanitize_input_names)
    graph = model.graph

    try:
        opset_in_model = 1
        if model.opset_import:
            # TODO: for now we only really support ai.onnx op set
            # TODO: handle other namespaces well see https://github.com/apache/tvm/issues/10950
            for opset_identifier in model.opset_import:
                # As per https://github.com/onnx/onnx/blob/main/docs/IR.md
                # All operator sets except the default one must specify the operator version
                if str(opset_identifier.domain) in ["ai.onnx", ""]:
                    opset_in_model = opset_identifier.version
                    break
    except AttributeError:
        opset_in_model = 1

    if opset is None:
        opset = opset_in_model
    elif opset < opset_in_model:
        warnings.warn(
            ""
            f"You are overwritting original opset ver = {opset_in_model} by lower ver = {opset}. "
            f"That might cause model conversion errors."
        )

    # Use the graph proto as a scope so that ops can access other nodes if needed.
    return g.from_onnx(graph, opset)
