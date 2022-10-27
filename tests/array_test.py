# Copyright 2021 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for GlobalDeviceArray."""

import os
from absl.testing import absltest
from absl.testing import parameterized
import numpy as np

import jax
import jax.numpy as jnp
from jax._src import dispatch
from jax._src import test_util as jtu
from jax._src.lib import xla_client as xc
from jax._src.lib import xla_bridge as xb
from jax._src.lib import xla_extension_version
from jax._src.util import prod, safe_zip
from jax.interpreters import pxla
from jax.experimental.pjit import pjit
from jax.experimental import PartitionSpec as P
from jax._src import sharding
from jax._src import array
from jax.experimental import maps

from jax.config import config
config.parse_flags_with_absl()


prev_xla_flags = None

# Run all tests with 8 CPU devices.
def setUpModule():
  global prev_xla_flags
  prev_xla_flags = os.getenv("XLA_FLAGS")
  flags_str = prev_xla_flags or ""
  # Don't override user-specified device count, or other XLA flags.
  if "xla_force_host_platform_device_count" not in flags_str:
    os.environ["XLA_FLAGS"] = (flags_str +
                               " --xla_force_host_platform_device_count=8")
  # Clear any cached backends so new CPU backend will pick up the env var.
  xb.get_backend.cache_clear()

# Reset to previous configuration in case other test modules will be run.
def tearDownModule():
  if prev_xla_flags is None:
    del os.environ["XLA_FLAGS"]
  else:
    os.environ["XLA_FLAGS"] = prev_xla_flags
  xb.get_backend.cache_clear()


def create_array(shape, sharding, global_data=None):
  if global_data is None:
    global_data = np.arange(prod(shape)).reshape(shape)

  return array.make_array_from_callback(
      shape, sharding, lambda idx: global_data[idx]), global_data


@jtu.with_config(jax_array=True)
class JaxArrayTest(jtu.JaxTestCase):

  @parameterized.named_parameters(
      ("mesh_x_y", P("x", "y")),
      ("mesh_x", P("x")),
      ("mesh_y", P("y")),
      ("mesh_none_y", P(None, "y")),
      ("mesh_xy", P(("x", "y"))),
      ("mesh_fully_replicated", P()),
  )
  def test_jax_array_value(self, mesh_axes):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    arr, global_data = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, mesh_axes))
    for s in arr.addressable_shards:
      self.assertLen(s.data._arrays, 1)
      self.assertArraysEqual(s.data._arrays[0], global_data[s.index])
    self.assertArraysEqual(arr._value, global_data)
    self.assertArraysEqual(arr._npy_value, global_data)

  @parameterized.named_parameters(
      ("mesh_x_y", P("x", "y"),
       # There are more slices but for convienient purposes, checking for only
       # 2. The indices + shard_shape + replica_id should be unique enough.
       ((slice(0, 2), slice(0, 1)), (slice(0, 2), slice(1, 2))),
       (2, 1),
       [0, 0, 0, 0, 0, 0, 0, 0], False),
      ("mesh_x", P("x"),
       ((slice(0, 2), slice(None)), (slice(0, 2), slice(None))),
       (2, 2),
       [0, 1, 0, 1, 0, 1, 0, 1], False),
      ("mesh_y", P("y"),
       ((slice(0, 4), slice(None)), (slice(4, 8), slice(None))),
       (4, 2),
       [0, 0, 1, 1, 2, 2, 3, 3], False),
      ("mesh_none_y", P(None, "y"),
       ((slice(None), slice(0, 1)), (slice(None), slice(1, 2))),
       (8, 1),
       [0, 0, 1, 1, 2, 2, 3, 3], False),
      ("mesh_xy", P(("x", "y")),
       ((slice(0, 1), slice(None)), (slice(1, 2), slice(None))),
       (1, 2),
       [0, 0, 0, 0, 0, 0, 0, 0], False),
      ("mesh_fully_replicated", P(),
       ((slice(None), slice(None)), (slice(None), slice(None))),
       (8, 2),
       [0, 1, 2, 3, 4, 5, 6, 7], True),
  )
  def test_array_2d_shard(self, mesh_axes, expected_index, expected_shard_shape,
                          expected_replica_ids, expected_is_fully_replicated):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    global_input_shape = (8, 2)
    s = sharding.MeshPspecSharding(global_mesh, mesh_axes)
    arr, global_input_data = create_array(global_input_shape, s)
    self.assertEqual(arr.ndim, 2)
    self.assertEqual(arr.size, 16)
    self.assertEqual(arr.addressable_shards[0].index, expected_index[0])
    self.assertEqual(arr.addressable_shards[1].index, expected_index[1])
    replica_ids = [i.replica_id for i in arr.addressable_shards]
    self.assertListEqual(replica_ids, expected_replica_ids)
    self.assertListEqual([i.device.id for i in arr.addressable_shards],
                         [0, 1, 2, 3, 4, 5, 6, 7])
    self.assertEqual(arr.is_fully_replicated, expected_is_fully_replicated)
    for i, s in enumerate(arr.addressable_shards):
      self.assertEqual(s.data.aval,
                       jax.ShapedArray(expected_shard_shape, s.data.dtype))
      self.assertArraysEqual(s.data, global_input_data[s.index])
      self.assertArraysEqual(s.data, arr.addressable_data(i))

    for g, l in safe_zip(arr.global_shards, arr.addressable_shards):
      self.assertEqual(g.device, l.device)
      self.assertEqual(g.index, l.index)
      self.assertEqual(g.replica_id, l.replica_id)
      self.assertEqual(g.data.aval, l.data.aval)
      self.assertArraysEqual(g.data, l.data)

  def test_addressable_data(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    shape = (8, 2)
    s = sharding.MeshPspecSharding(global_mesh, P(None))
    arr, inp_data = create_array(shape, s)
    for i in range(len(arr)):
      self.assertArraysEqual(inp_data, arr.addressable_data(i))

  def test_array_delete(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    arr, _ = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P('x', 'y')))
    arr.delete()
    with self.assertRaisesRegex(RuntimeError, 'Array has been deleted.'):
      arr._check_if_deleted()
    self.assertIsNone(arr._npy_value)
    self.assertIsNone(arr._arrays)

  def test_single_device_array_usage_after_delete(self):
    x = jnp.array([1, 2, 3])
    x.delete()

    with self.assertRaisesRegex(RuntimeError, 'Array has been deleted.'):
      _ = x + 1

  def test_multi_device_array_usage_after_delete(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    arr, _ = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P('x', 'y')))
    arr.delete()

    with self.assertRaisesRegex(RuntimeError, 'Array has been deleted.'):
      _ = arr + 1

  def test_device_put(self):
    numpy_array = np.array([1, 2, 3])
    arr = jax.device_put(numpy_array, jax.devices()[0])
    self.assertIsInstance(arr.sharding, sharding.SingleDeviceSharding)
    self.assertArraysEqual(arr, numpy_array)
    self.assertEqual(arr._committed, True)
    for i in arr.addressable_shards:
      self.assertArraysEqual(i.data, numpy_array)
      self.assertEqual(i.device, jax.devices()[0])
      self.assertEqual(i.index, (slice(None),))
      self.assertEqual(i.replica_id, 0)

  def test_device_put_array_delete(self):
    arr = jax.device_put(np.array([1, 2, 3]), jax.devices()[0])
    arr.delete()
    with self.assertRaisesRegex(RuntimeError, 'Array has been deleted.'):
      arr._check_if_deleted()
    self.assertIsNone(arr._npy_value)
    self.assertIsNone(arr._arrays)

  def test_array_device_get(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    arr, input_data = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P('x', 'y')))
    self.assertArraysEqual(jax.device_get(arr), input_data)

  def test_repr(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    arr, _ = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P('x', 'y')))
    self.assertStartsWith(repr(arr), "Array(")

  def test_jnp_array(self):
    arr = jnp.array([1, 2, 3])
    self.assertIsInstance(arr, array.ArrayImpl)
    self.assertTrue(dispatch.is_single_device_sharding(arr.sharding))
    self.assertEqual(arr._committed, False)
    self.assertFalse(arr.weak_type)

  def test_jnp_array_jit_add(self):
    a = jnp.array([1, 2, 3])
    b = jnp.array([4, 5, 6])
    arr = jax.jit(lambda x, y: x + y)(a, b)
    self.assertIsInstance(arr, array.ArrayImpl)
    self.assertArraysEqual(arr, np.array([5, 7, 9]))
    self.assertIsInstance(arr.sharding, sharding.SingleDeviceSharding)

  def test_jnp_array_jnp_add(self):
    arr = jnp.add(jnp.array([1, 2, 3]), jnp.array([4, 5, 6]))
    self.assertIsInstance(arr, array.ArrayImpl)
    self.assertArraysEqual(arr, np.array([5, 7, 9]))
    self.assertIsInstance(arr.sharding, sharding.SingleDeviceSharding)

  def test_jnp_array_normal_add(self):
    a = jnp.array([1, 2, 3])
    b = jnp.array([4, 5, 6])
    arr = a + b
    self.assertIsInstance(arr, array.ArrayImpl)
    self.assertArraysEqual(arr, np.array([5, 7, 9]))
    self.assertIsInstance(arr.sharding, sharding.SingleDeviceSharding)

  def test_array_sharded_astype(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    arr, input_data = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P('x', 'y')))
    arr_float32 = arr.astype(jnp.float32)
    self.assertEqual(arr_float32.dtype, np.float32)
    self.assertArraysEqual(arr_float32, input_data.astype(np.float32))
    self.assertLen(arr_float32.addressable_shards, 8)
    for i in arr_float32.addressable_shards:
      self.assertArraysEqual(i.data, input_data[i.index].astype(np.float32))

  def test_jnp_array_astype(self):
    arr = jnp.array([1, 2, 3])
    arr_float32 = arr.astype(jnp.float32)
    self.assertEqual(arr_float32.dtype, np.float32)
    self.assertArraysEqual(arr_float32, arr.astype(np.float32))

  def test_sharded_add(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    a, input_data = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P('x', 'y')))
    b, _ = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P('x')))
    out = a + b
    expected = input_data + input_data
    self.assertArraysEqual(out, expected)
    self.assertLen(out.addressable_shards, 8)
    for i in out.addressable_shards:
      self.assertArraysEqual(i.data, expected[i.index])

  def test_sharded_zeros_like(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    a, input_data = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P('x', 'y')))
    out = jnp.zeros_like(a)
    expected = jnp.zeros(input_data.shape, dtype=int)
    self.assertArraysEqual(out, expected)
    self.assertLen(out.addressable_shards, 8)
    for i in out.addressable_shards:
      self.assertArraysEqual(i.data, expected[i.index])

  def test_zeros_like(self):
    a = jnp.array([1, 2, 3], dtype=np.int32)
    out = jnp.zeros_like(a)
    expected = np.zeros(a.shape, dtype=np.int32)
    self.assertArraysEqual(out, expected)
    self.assertTrue(dispatch.is_single_device_sharding(out.sharding))

  def test_wrong_num_arrays(self):
    shape = (8, 2)
    mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    s = sharding.MeshPspecSharding(mesh, P('x', 'y'))
    inp_data = np.arange(prod(shape), dtype=np.float32).reshape(shape)
    bufs = [jax.device_put(inp_data[s.device_indices(d, shape)], d)
            for d in jax.local_devices()]
    with self.assertRaisesRegex(
        ValueError,
        r'Expected 8 per-device arrays \(this is how many devices are addressable '
        r'by the sharding\), but got 4'):
      array.ArrayImpl(jax.ShapedArray(shape, np.float32), s, bufs[:4], committed=True)

    with self.assertRaisesRegex(
        ValueError,
        r'Expected 8 per-device arrays \(this is how many devices are addressable '
        r'by the sharding\), but got 16'):
      array.ArrayImpl(jax.ShapedArray(shape, np.float32), s, bufs + bufs, committed=True)

  def test_arrays_not_in_device_assignment(self):
    if jax.device_count() < 4:
      self.skipTest('Requires more than 4 devices')
    shape = (8, 2)
    mesh = jtu.create_global_mesh((1, 2), ('x', 'y'))
    # sharding device ids = {0, 1}
    s = sharding.MeshPspecSharding(mesh, P('x'))
    inp_data = np.arange(prod(shape), dtype=np.float32).reshape(shape)
    # _arrays device ids = {2, 3}
    bufs = [jax.device_put(inp_data, d) for d in jax.devices()[2:4]]
    with self.assertRaisesRegex(
        ValueError,
        "Addressable devices and per-device arrays devices do not match. "
        "Sharding contains devices {0, 1} that are not present in per-device "
        "arrays. Per-device arrays contain devices {2, 3} that are not present "
        "in the sharding."):
      array.ArrayImpl(jax.ShapedArray(shape, np.float32), s, bufs, committed=True)

  def test_more_devices_in_sharding_than_arrays(self):
    shape = (8, 2)
    mesh = jtu.create_global_mesh((1, 2), ('x', 'y'))
    # Sharding device ids = {0, 1}
    s = sharding.MeshPspecSharding(mesh, P('x'))
    inp_data = np.arange(prod(shape), dtype=np.float32).reshape(shape)
    # _arrays device ids = {0, 0}
    bufs = [jax.device_put(inp_data, jax.devices()[0]) for _ in range(2)]
    with self.assertRaisesRegex(
        ValueError,
        "Addressable devices and per-device arrays devices do not match. "
        r"Sharding contains devices \{1\} that are not present in per-device "
        "arrays."):
      array.ArrayImpl(jax.ShapedArray(shape, np.float32), s, bufs, committed=True)

  def test_different_devices_in_arrays_than_sharding(self):
    if jax.device_count() < 3:
      self.skipTest('Requires more than 3 devices')
    shape = (8, 2)
    mesh = maps.Mesh(np.array([jax.devices()[1], jax.devices()[2]]), ('x'))
    # sharding device ids = {1, 2}
    s = sharding.MeshPspecSharding(mesh, P('x'))
    inp_data = np.arange(prod(shape), dtype=np.float32).reshape(shape)
    # _arrays device ids = {0, 1}
    bufs = [jax.device_put(inp_data, d) for d in jax.devices()[:2]]
    with self.assertRaisesRegex(
        ValueError,
        "Addressable devices and per-device arrays devices do not match. "
        r"Sharding contains devices \{2\} that are not present in per-device "
        r"arrays. Per-device arrays contain devices \{0\} that are not present "
        "in the sharding."):
      array.ArrayImpl(jax.ShapedArray(shape, np.float32), s, bufs, committed=True)

  @parameterized.named_parameters(
      ("mesh_x_y", P("x", "y"), (2, 2)),
      ("mesh_x", P("x"), (2, 4)),
      ("mesh_y", P("y"), (4, 4)),
      ("mesh_none_y", P(None, "y"), (8, 2)),
      ("mesh_none_x", P(None, "x"), (8, 1)),
      ("mesh_xy", P(("x", "y")), (1, 4)),
  )
  def test_shard_shape_mismatch_with_buffer_shape(self, pspec, expected_shard_shape):
    shape = (8, 4)
    mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    mps = sharding.MeshPspecSharding(mesh, pspec)
    inp_data = np.arange(prod(shape)).reshape(shape)

    str_expected_shard_shape = str(expected_shard_shape).replace(
        r"(", r"\(").replace(r")", r"\)")
    with self.assertRaisesRegex(
        ValueError,
        f"Expected shard shape {str_expected_shard_shape} doesn't match the "
        "buffer shape"):
      array.make_array_from_callback(shape, mps, lambda idx: inp_data)

  def test_mismatch_dtype(self):
    shape = (8, 2)
    mesh = jtu.create_global_mesh((1, 2), ('x', 'y'))
    s = sharding.MeshPspecSharding(mesh, P('x', 'y'))
    inp_data = np.arange(prod(shape), dtype=np.int32).reshape(shape)
    indices = s.devices_indices_map(shape)
    bufs = [jax.device_put(inp_data[indices[d]], d) for d in mesh.local_devices]
    with self.assertRaisesRegex(
        ValueError,
        "Input buffers to `Array` must have matching dtypes. "
        "Got int32, expected float32"):
      array.ArrayImpl(jax.ShapedArray(shape, np.float32), s, bufs, committed=True)

  def test_array_iter_pmap_sharding(self):
    if jax.device_count() < 2:
      self.skipTest('Test requires >= 2 devices.')

    x = jnp.array([[1., 0., 0.], [0., 2., 3.]])
    y = jax.pmap(jnp.sin)(x)
    self.assertArraysEqual([a.device() for a in y],
                           [a.device() for a in y._arrays])

    sin_x = iter(np.sin(x))
    for i, j in zip(iter(y), sin_x):
      self.assertIsInstance(i, array.ArrayImpl)
      self.assertArraysAllClose(i, j)

  def test_array_iter_pmap_sharding_last_dim_sharded(self):
    if jax.device_count() < 2:
      self.skipTest('Test requires >= 2 devices.')

    x = jnp.array([[1., 0., 0.], [0., 2., 3.]])
    y = jax.pmap(jnp.sin, out_axes=1)(x)

    for i, j in zip(iter(y), iter(np.sin(x).T)):
      self.assertArraysAllClose(i, j)

  def test_array_iter_mesh_pspec_sharding_multi_device(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    arr, input_data = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P('x', 'y')))

    for i, j in zip(iter(arr), iter(input_data)):
      self.assertIsInstance(i, array.ArrayImpl)
      self.assertArraysEqual(i, j)

  def test_array_iter_replicated_multi_device(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    arr, input_data = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P(None)))

    for i, j in zip(iter(arr), iter(input_data)):
      self.assertIsInstance(i, array.ArrayImpl)
      self.assertArraysEqual(i, j)
      self.assertLen(i.sharding.device_set, 8)
      self.assertTrue(
        pxla.are_op_shardings_equal(
            arr.sharding._to_xla_op_sharding(arr.ndim),
            i.sharding._to_xla_op_sharding(i.ndim)))

  def test_array_getitem_mesh_pspec_sharding_multi_device(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    arr, input_data = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P('x', 'y')))

    # TODO(yashkatariya): `__getitem__` with a specific index takes the fast
    # path after b/245667823 is fixed.
    s = arr[2:4, 0:1]
    self.assertIsInstance(s, array.ArrayImpl)
    self.assertArraysEqual(s, np.array([[4], [6]]))

    p = arr[:2]
    self.assertIsInstance(p, array.ArrayImpl)
    self.assertArraysEqual(p, input_data[:2])

  def test_array_getitem_replicated_multi_device(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    arr, input_data = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P(None)))

    s = arr[2:4, 0:1]
    self.assertIsInstance(s, array.ArrayImpl)
    self.assertArraysEqual(s, np.array([[4], [6]]))
    self.assertLen(s.sharding.device_set, 8)
    self.assertTrue(
        pxla.are_op_shardings_equal(
            arr.sharding._to_xla_op_sharding(arr.ndim),
            s.sharding._to_xla_op_sharding(s.ndim)))

    p = arr[:2]
    self.assertIsInstance(p, array.ArrayImpl)
    self.assertArraysEqual(p, input_data[:2])
    self.assertLen(s.sharding.device_set, 8)
    self.assertTrue(
        pxla.are_op_shardings_equal(
            arr.sharding._to_xla_op_sharding(arr.ndim),
            s.sharding._to_xla_op_sharding(s.ndim)))

  def test_array_iter_mesh_pspec_sharding_single_device(self):
    if jax.device_count() < 2:
      self.skipTest('Test requires >= 2 devices.')

    single_dev = jax.devices()[1:2]
    mesh = maps.Mesh(np.array(single_dev), ('x'))
    input_shape = (8, 2)
    arr, input_data = create_array(
        input_shape, sharding.MeshPspecSharding(mesh, P('x')))

    for i, j in zip(arr, iter(input_data)):
      self.assertArraysEqual(i, j)
      self.assertEqual(i.device(), single_dev[0])

  def test_array_shards_committed(self):
    if jax.device_count() < 2:
      self.skipTest('Test requires >= 2 devices.')

    x = jnp.array([1, 2, 3])
    for s in x.addressable_shards:
      self.assertEqual(s.data._committed, x._committed)
      self.assertFalse(s.data._committed)

    y = jax.device_put(x, jax.devices()[1])
    for s in y.addressable_shards:
      self.assertEqual(s.data._committed, y._committed)
      self.assertTrue(s.data._committed)

  def test_array_jnp_array_copy_multi_device(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    arr, _ = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P('x', 'y')))

    c_arr = jnp.array(arr, copy=True)
    self.assertArraysEqual(arr, c_arr)
    self.assertEqual(arr._committed, c_arr._committed)
    for a, c in safe_zip(arr.addressable_shards, c_arr.addressable_shards):
      self.assertArraysEqual(a.data, c.data)
      self.assertEqual(a.index, c.index)
      self.assertEqual(a.replica_id, c.replica_id)
      self.assertEqual(a.device, c.device)
      self.assertNotEqual(a.data.unsafe_buffer_pointer(),
                          c.data.unsafe_buffer_pointer())

  def test_array_device_buffer(self):
    global_mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    input_shape = (8, 2)
    arr, _ = create_array(
        input_shape, sharding.MeshPspecSharding(global_mesh, P('x', 'y')))

    for a in arr.device_buffers:
      self.assertIsInstance(a, array.ArrayImpl)

    x = jnp.array([1, 2, 3])
    self.assertIsInstance(x.device_buffer, array.ArrayImpl)

  def test_shape_dtype_struct_sharding_jit(self):
    mesh = jtu.create_global_mesh((8,), ('x'))
    s = sharding.MeshPspecSharding(mesh, P('x'))

    x_dummy = jax.ShapeDtypeStruct(
        shape=(16,),
        dtype=jnp.dtype('float32'),
        sharding=s)

    def f(x):
      return x * 2

    c = jax.jit(f).lower(x_dummy).compile()
    input_shardings, output_shardings = c.input_shardings, c.output_shardings
    self.assertLen(input_shardings, 2)
    self.assertEqual(input_shardings[1], {})
    self.assertEqual(input_shardings[1], {})

    self.assertTrue(
        pxla.are_op_shardings_equal(
            input_shardings[0][0]._to_xla_op_sharding(x_dummy.ndim),
            s._to_xla_op_sharding(x_dummy.ndim)))
    self.assertTrue(
        pxla.are_op_shardings_equal(
            output_shardings._to_xla_op_sharding(x_dummy.ndim),
            s._to_xla_op_sharding(x_dummy.ndim)))

  def test_shape_dtype_struct_sharding_pjit(self):
    mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    s = sharding.MeshPspecSharding(mesh, P('x', 'y'))

    def f(x):
      return x * 2.

    x_dummy = jax.ShapeDtypeStruct(
        shape=(8, 2),
        dtype=jnp.dtype('float32'),
        sharding=s)

    c = pjit(f).lower(x_dummy).compile()
    input_shardings, output_shardings = c.input_shardings, c.output_shardings
    self.assertTrue(
        pxla.are_op_shardings_equal(
            input_shardings[0][0]._to_xla_op_sharding(x_dummy.ndim),
            s._to_xla_op_sharding(x_dummy.ndim)))
    self.assertTrue(
        pxla.are_op_shardings_equal(
            output_shardings._to_xla_op_sharding(x_dummy.ndim),
            s._to_xla_op_sharding(x_dummy.ndim)))



@jtu.with_config(jax_array=True)
class ShardingTest(jtu.JaxTestCase):

  def test_mesh_pspec_sharding_interface(self):
    mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    pspec = P('y', 'x')
    global_shape = (8, 4)
    mp_sharding = sharding.MeshPspecSharding(mesh, pspec)
    di_map = mp_sharding.devices_indices_map(global_shape)
    op_sharding = mp_sharding._to_xla_op_sharding(len(global_shape))
    device_assignment = mp_sharding._device_assignment

    self.assertEqual(di_map[mesh.devices.flat[0]], (slice(0, 4), slice(0, 1)))
    self.assertArraysEqual(device_assignment, list(mesh.devices.flat))
    self.assertEqual(op_sharding.type, xc.OpSharding.Type.OTHER)
    self.assertListEqual(op_sharding.tile_assignment_dimensions, [2, 4])
    self.assertListEqual(op_sharding.tile_assignment_devices,
                         [0, 2, 4, 6, 1, 3, 5, 7])

  @parameterized.named_parameters(
      ("mesh_x_y", P("x", "y")),
      ("mesh_x", P("x")),
      ("mesh_y", P("y")),
      ("mesh_none_y", P(None, "y")),
      ("mesh_none_x", P(None, "x")),
      ("mesh_xy", P(("x", "y"))),
      ("mesh_fully_replicated", P()),
  )
  def test_op_sharding_indices(self, pspec):
    shape = (8, 4)
    mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    mps = sharding.MeshPspecSharding(mesh, pspec)
    ops = sharding.OpShardingSharding(
        list(mesh.devices.flat), mps._to_xla_op_sharding(len(shape)))
    self.assertDictEqual(
        ops.devices_indices_map(shape), mps.devices_indices_map(shape))

  @parameterized.named_parameters(
      ("mesh_x_y", P("x", "y"), (2, 2)),
      ("mesh_x", P("x"), (2, 4)),
      ("mesh_y", P("y"), (4, 4)),
      ("mesh_none_y", P(None, "y"), (8, 2)),
      ("mesh_none_x", P(None, "x"), (8, 1)),
      ("mesh_xy", P(("x", "y")), (1, 4)),
      ("mesh_fully_replicated", P(), (8, 4)),
  )
  def test_shard_shape(self, pspec, expected_shard_shape):
    shape = (8, 4)
    mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    mps = sharding.MeshPspecSharding(mesh, pspec)
    self.assertEqual(mps.shard_shape(shape), expected_shard_shape)

  def test_uneven_shard_error(self):
    mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    mps = sharding.MeshPspecSharding(mesh, P('x', 'y'))
    with self.assertRaisesRegex(
        ValueError,
        r"Sharding.*implies that array axis 1 is partitioned 2 times, but the "
        r"dimension size is 3 \(full shape: \(8, 3\), per-dimension tiling "
        r"factors: \[4, 2\] should evenly divide the shape\)"):
      mps.shard_shape((8, 3))

  def test_pmap_sharding_hash_eq(self):
    if jax.device_count() < 2:
      self.skipTest('Test needs >= 2 devices.')

    shape = (2, 2)
    num_elements = prod(shape)
    inp_data = np.arange(num_elements).reshape(shape)
    out = jax.pmap(lambda x: x)(inp_data)
    self.assertIsInstance(out.sharding, sharding.PmapSharding)
    # Populate the device_indices_map cache.
    _ = out.sharding.devices_indices_map(shape)
    cache_info1 = sharding.PmapSharding.devices_indices_map.cache_info()

    inp_data2 = np.arange(num_elements, num_elements + num_elements).reshape(shape)
    out2 = jax.pmap(lambda x: x)(inp_data2)
    # Populate the device_indices_map cache.
    _ = out2.sharding.devices_indices_map(shape)
    cache_info2 = sharding.PmapSharding.devices_indices_map.cache_info()

    self.assertGreater(cache_info2.hits, cache_info1.hits + 1)
    self.assertEqual(cache_info2.misses, cache_info1.misses)

  def test_is_compatible_error(self):
    shape = (8, 2)
    mesh = jtu.create_global_mesh((1, 1, 2), ('replica', 'data', 'mdl'))
    mps = sharding.MeshPspecSharding(mesh, P(None, ('mdl',), None, None))
    new_mps = sharding.MeshPspecSharding._from_parsed_pspec(
        mps.mesh, mps._parsed_pspec)

    with self.assertRaisesRegex(
        ValueError,
        r"Sharding MeshPspecSharding\(mesh={'replica': 1, 'data': 1, 'mdl': 2}, "
        r"partition_spec=PartitionSpec\(None, \('mdl',\), None, None\)\) is only "
        "valid for values of rank at least 4, but was applied to a value of rank 2"):
      new_mps.is_compatible_aval(shape)

  def test_is_subclass(self):
    # array version of api_test.py::APITest::test_is_subclass
    self.assertTrue(issubclass(array.ArrayImpl, jnp.ndarray))
    self.assertFalse(issubclass(array.ArrayImpl, np.ndarray))

  def test_op_sharding_sharding_repr(self):
    if xla_extension_version < 96:
      self.skipTest('This test only works with the latest HloSharding repr.')
    op = xc.OpSharding()
    op.type = xc.OpSharding.Type.OTHER
    op.tile_assignment_dimensions = [4, 1, 2]
    op.tile_assignment_devices = [0, 1, 2, 3, 4, 5, 6, 7]
    op.replicate_on_last_tile_dim = True
    s = sharding.OpShardingSharding(jax.devices(), op)
    self.assertEqual(
        repr(s),
        'OpShardingSharding({devices=[4,1,2]0,1,2,3,4,5,6,7 '
        'last_tile_dim_replicate})')

    op2 = xc.OpSharding()
    op2.type = xc.OpSharding.Type.REPLICATED
    s2 = sharding.OpShardingSharding(jax.devices(), op2)
    self.assertEqual(repr(s2), 'OpShardingSharding({replicated})')

  @parameterized.named_parameters(
      ("mesh_x_y",              P("x", "y"),   (4, 2), (),   False),
      ("mesh_x",                P("x"),        (4, 2), (1,), False),
      ("mesh_y",                P("y"),        (4, 2), (0,), True),
      ("mesh_none_y",           P(None, "y"),  (4, 2), (0,), False),
      ("mesh_none_x",           P(None, "x"),  (4, 2), (1,), True),
      ("mesh_xy",               P(("x", "y")), (8, 1), (),   False),
      ("mesh_fully_replicated", P(),           (4, 2), None, False),
  )
  def test_devices_sharding_op_sharding_lowering(
      self, pspec, shape, axes, transpose):
    value_shape = (8, 4)

    mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    mps = sharding.MeshPspecSharding(mesh, pspec)

    devices_sharding = sharding.ReshapeableDevicesSharding(jax.devices())
    devices_sharding = devices_sharding.reshape(shape).replicate(axes)
    if transpose:
      devices_sharding = devices_sharding.T

    op1 = mps._to_xla_op_sharding(len(value_shape))
    op2 = devices_sharding._to_xla_op_sharding(len(value_shape))

    self.assertEqual(mps.shard_shape(value_shape),
                     devices_sharding.shard_shape(value_shape))
    self.assertTrue(pxla.are_op_shardings_equal(op1, op2))

  def test_devices_sharding_respects_init_mesh_shape(self):
    value_shape = (8, 4)

    mesh = jtu.create_global_mesh((4, 2), ('x', 'y'))
    mps = sharding.MeshPspecSharding(mesh, P('x', 'y'))

    devices_sharding = sharding.ReshapeableDevicesSharding(mesh.devices)

    op1 = mps._to_xla_op_sharding(len(value_shape))
    op2 = devices_sharding._to_xla_op_sharding(len(value_shape))

    self.assertEqual(mps.shard_shape(value_shape),
                     devices_sharding.shard_shape(value_shape))
    self.assertTrue(pxla.are_op_shardings_equal(op1, op2))

  def test_pmap_sharding_repr(self):
    if jax.device_count() < 2:
      self.skipTest('Test needs >= 2 devices.')
    out = jax.pmap(lambda x: x)(jnp.arange(2.))
    str(out.sharding)  # doesn't crash
    repr(out.sharding)  # doesn't crash


if __name__ == '__main__':
  absltest.main(testLoader=jtu.JaxTestLoader())
