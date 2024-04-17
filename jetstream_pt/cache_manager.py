# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch_xla2
import jax
import jax.numpy as jnp
import torch


class CacheInterface:
    # cache for ONE layer

    def update(self, key, value):
        """Update the cache for this key and value.
        
        The key, and val will have shape (Batch, Heads, Seqlen, Head dim)
        The cache is free to store them in a different format.
        Return the full cache after update.
        This cache instance need to know which position / layer is 
        the update for.
        """

class KVCachePrefill:

    def __init__(self, kv_quantize=False):
        self.kv_quantize = kv_quantize 
        self.cache_k = None
        self.cache_v = None

    def update(self, key, value):
        """This cache just remembers the stuff."""
        self.cache_k = key
        self.cache_v = value
        if self.kv_quantize:  # pretend to be quantized
            bsz, _, seq, _ = key.shape
            ones = torch_xla2.tensor.wrap(jnp.ones((bsz, 1, seq, 1), dtype=jnp.bfloat16))
            return key, value, ones, ones
        else:
            return key, value

    def state(self):
        return self.cache_k, self.cache_v


def KVCachePrefill_flatten(cache):
    return torch_xla2.tensor.unwrap((cache.cache_k, cache.cache_v)), cache.kv_quantize


def KVCachePrefill_unflatten(auxdata, data):
    cache = KVCachePrefill(auxdata)
    cache_k, cache_v = torch_xla2.tensor.wrap(data)
    cache.cache_k = cache_k
    cache.cache_v = cache_v


jax.tree_util.register_pytree_node(
    KVCachePrefill, 
    KVCachePrefill_flatten, 
    KVCachePrefill_unflatten)




# Refactor out cache management
# Easier to test for quantized kv cache
class KVCacheGenerate:

    def __init__(self, 
        cache_k: torch.Tensor,  # previous cache
        cache_v: torch.Tensor,  # previous cache
        position: torch.Tensor,  # position to store the cache
        sharding,
    ):
        super().__init__()
        self.cache_k = cache_k
        self.cache_v = cache_v
        self.pos = position
        self.batch = torch.arange(position.shape[0])
        self.sharding = sharding

    def update(self, key, value):
        keyj, valuej = torch_xla2.tensor.unwrap((key, value))
        self.cache_k._elem = self.cache_k._elem.at[self.batch, :, self.pos].set(torch.squeeze(keyj, 2))
        self.cache_v._elem = self.cache_v._elem.at[self.batch, :, self.pos].set(torch.squeeze(valuej, 2))
        return self.cache_k, self.cache_v 

    def state(self):
        return self.cache_k._elem, self.cache_v._elem

    @classmethod
    def empty(cls, shape, device, bf16_enable):
        default_dtype = jnp.bfloat16 if bf16_enable else jnp.float32
        k = jnp.zeros(shape, device=device, dtype=default_dtype)
        v = jnp.zeros(shape, device=device, dtype=default_dtype)
        k, v = torch_xla2.tensor.wrap((k, v))
        pos = jnp.array([0])  # replicated
        return cls(k, v, 0, device)

def KVCacheGenerate_flatten(cache):
    return torch_xla2.tensor.unwrap((cache.cache_k, cache.cache_v)), (cache.pos, cache.sharding)


def KVCacheGenerate_unflatten(auxdata, data):
    position, sharding = auxdata
    cache_k, cache_v = torch_xla2.tensor.wrap(data)
    cache = KVCacheGenerate(cache_k, cache_v, position, sharding)
    return cache


jax.tree_util.register_pytree_node(
    KVCacheGenerate, 
    KVCacheGenerate_flatten, 
    KVCacheGenerate_unflatten)
        

class Int8KVCacheGenerate:

    def __init__(self, 
        cache_k, 
        cache_v, 
        cache_k_scaler,
        cache_v_scaler, 
        input_pos,  # used to write cache
        sharding = None,
    ):
        super().__init__()
        self.cache_k = cache_k
        self.cache_v = cache_v
        self.k_scaler = cache_k_scaler 
        self.v_scaler = cache_v_scaler 
        self.input_pos = input_pos
        self.batch = torch.arange(input_pos.shape[0])

    def state(self):
        return torch_xla2.tensor.unwrap((self.cache_k, self.cache_v))

    
    def scalers(self):
        return torch_xla2.tensor.unwrap((self.k_scaler, self.v_scaler))

    @classmethod
    def empty(cls, shape, device, bf16_enable):
        cache_k = jnp.zeros(shape, device=device, dtype=jnp.int8)
        cache_v = jnp.zeros(shape, device=device, dtype=jnp.int8)
        # bf16_enable is a placeholder parameter, it's not used in Int8KVCache 
        kscaler = jnp.ones((shape[0], 1, shape[2], 1), dtype=jnp.bfloat16)
        vscaler = jnp.ones((shape[0], 1, shape[2], 1), dtype=jnp.bfloat16)

        cache_k, cache_v, kscaler, vscaler = torch_xla2.tensor.wrap((cache_k, cache_v, kscaler, vscaler))
        return cls(cache_k, cache_v, kscaler, vscaler, 0, device)


    def quantize(self, val):
        # val is (batch, heads, seqlen, dim)
        scale = torch.amax(val.abs(), axis=(1, 3), keepdim=True)
        scale = scale / 127
        return (val / scale).to(torch.int8), scale

    def update(self, xk, xv):
        k_quant, kscale = self.quantize(xk)
        v_quant, vscale = self.quantize(xv)
        self.cache_k[self.batch, :, self.input_pos, :] = torch.squeeze(k_quant, 2)
        self.cache_v[self.batch, :, self.input_pos, :] = torch.squeeze(v_quant, 2)
        self.k_scaler[self.batch, :, self.input_pos, :] = torch.squeeze(kscale, 2)
        self.v_scaler[self.batch, :, self.input_pos, :] = torch.squeeze(vscale, 2)
        return self.cache_k, self.cache_v, self.k_scaler, self.v_scaler
