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

r"""Utility to merge sharded weights of llama2 model into a single file.

Usage:
export input_ckpt_dir=/path/to/llama2/weight/dir
export output_ckpt_dir=/tmp/llama2/
python convert_checkpoint.py \
    --input_checkpoint_dir=${input_ckpt_dir} \
    --output_checkpoint_dir=${output_ckpt_dir}
"""

import gc
import hashlib
import json
import os
import time

from absl import app
from absl import flags
from etils import epath

from safetensors.torch import save_file
import torch
from google.cloud import storage

from jetstream_pt import quantize


_INPUT_CHECKPOINT_DIR = epath.DEFINE_path(
    "input_checkpoint_dir",
    None,
    "The input dir containing llama2 model weights sharded across files.",
)

_OUTPUT_CHECKPOINT_DIR = epath.DEFINE_path(
    "output_checkpoint_dir",
    None,
    "The output dir containing llama2 model weights merged in a single file.",
)

_MINIMIZE_MEMORY_FOOTPRINT = flags.DEFINE_bool(
    "minimize_memory_footprint",
    False,
    "When set to true, reduce memory usage by staging in-memory data on disk",
)

_ENABLE_FLOAT32 = flags.DEFINE_bool(
    "enable_float32",
    False,
    "When set to true, convert to float32 weights",
)

_OUTPUT_SAFETENSORS = flags.DEFINE_bool(
    "output_safetensors",
    True,
    "When set to true, save to HugginFace SafeTensors format",
)
_QUANTIZE = flags.DEFINE_bool(
    "quantize", False, "When set to true, produces quantized weights"
)
_MODEL_TYPE = flags.DEFINE_string("model_name", "llama", "Type of the model.")

# ParallelEmbedding is col partitioned across the shards.
# ColumnParallelLinear is row partitioned across shards due to transpose.
# RowParallelLinear is col partitioned across shards due to transpose.
# None is no partitioning and tensor should be identical across shards
_WEIGHT_SHARDING_TYPE = {
    "tok_embeddings.weight": "ParallelEmbedding",
    "rope.freqs": None,
    "attention.wq.weight": "ColumnParallelLinear",
    "attention.wk.weight": "ColumnParallelLinear",
    "attention.wv.weight": "ColumnParallelLinear",
    "attention.wo.weight": "RowParallelLinear",
    "feed_forward.w1.weight": "ColumnParallelLinear",
    "feed_forward.w2.weight": "RowParallelLinear",
    "feed_forward.w3.weight": "ColumnParallelLinear",
    "attention_norm.weight": None,
    "ffn_norm.weight": None,
    "norm.weight": None,
    "output.weight": "ColumnParallelLinear",
}

_LLAMA_QUANTIZED_WEIGHTS_TO_SCALER_NAME = {
    "tok_embeddings.weight": "tok_embeddings.weight_scaler",
    "attention.wq.weight": "attention.wq.weight_scaler",
    "attention.wk.weight": "attention.wk.weight_scaler",
    "attention.wv.weight": "attention.wv.weight_scaler",
    "attention.wo.weight": "attention.wo.weight_scaler",
    "feed_forward.w1.weight": "feed_forward.w1.weight_scaler",
    "feed_forward.w2.weight": "feed_forward.w2.weight_scaler",
    "feed_forward.w3.weight": "feed_forward.w3.weight_scaler",
    "output.weight": "output.weight_scaler",
}

_GEMMA_QUANTIZED_WEIGHTS_TO_SCALER_NAME = {
    "self_attn.o_proj.weight": "self_attn.o_proj.weight_scaler",
    "self_attn.wq.weight": "self_attn.wq.weight_scaler",
    "self_attn.wk.weight": "self_attn.wk.weight_scaler",
    "self_attn.wv.weight": "self_attn.wv.weight_scaler",
    "mlp.gate_proj.weight": "mlp.gate_proj.weight_scaler",
    "mlp.up_proj.weight": "mlp.up_proj.weight_scaler",
    "mlp.down_proj.weight": "mlp.down_proj.weight_scaler",
    "embedder.weight": "embedder.weight_scaler",
}


def _quantize_state_dict(state_dict, weight_map, weight_axis):
  updated_weights = {}
  for key, val in state_dict.items():
    for qname, qscale_name in weight_map.items():
      if key.endswith(qname):
        new_weights, scaler = quantize.quantize_torch_int8(
            val, reduce_axis=(weight_axis(key),)
        )
        updated_weights[key] = new_weights
        scale_name = key[: -len(qname)] + qscale_name
        updated_weights[scale_name] = scaler.squeeze()
  state_dict.update(updated_weights)
  return state_dict


def _compute_md5(file_path: epath.Path) -> str:
  md5_hash = hashlib.md5()
  with file_path.open("rb") as file:
    # Use larger buffer for better read throughput,
    # since checkpoint file is typically tens of GBs in size.
    while data := file.read(256 * 1024):
      md5_hash.update(data)
  return md5_hash.hexdigest()


def _generate_md5_checklist(target_dir: epath.Path) -> None:
  files = [target_dir / file for file in target_dir.iterdir() if file.is_file()]
  return "\n".join([f"{_compute_md5(f)}\n" for f in files]) + "\n"


def _checkpoints_have_same_weight_keys(
    checkpoint_list: list[dict[str, torch.Tensor]]
):
  if (not checkpoint_list) or len(checkpoint_list) <= 1:
    return True
  for m in checkpoint_list[1:]:
    if set(checkpoint_list[0].keys()) != set(m.keys()):
      return False
  return True


def _tensors_have_same_shape(tensors):
  if (not tensors) or len(tensors) <= 1:
    return True
  for t in tensors[1:]:
    if t.shape != tensors[0].shape:
      return False
  return True


# pylint: disable-next=all
def _merge_llama_weights(
    checkpoints, minimize_memory_footprint, enable_float32
):
  print("Starting to merge weights.")
  state_dict = {}
  tmp_dir: epath.Path = None
  if minimize_memory_footprint:
    # tmp_dir = output_ckpt_dir / 'tmp'
    # Store the temp data locally
    tmp_dir = epath.Path("/tmp/checkpoints")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    print(f"Stage in-memory data on disk {tmp_dir} to reduce memory uage")
  if not _checkpoints_have_same_weight_keys(checkpoints):
    raise ValueError("Checkpoint must have the same set of weights.")
  weight_keys = checkpoints[0].keys()
  for key in weight_keys:
    tensors: list[torch.Tensor] = [c[key] for c in checkpoints]
    if not _tensors_have_same_shape(tensors):
      raise ValueError(f"Tensors must have the same shape for {key}")
    print(
        "Merging weights across "
        f"{len(tensors)} shards (shape = {tensors[0].shape}) for {key})"
    )
    state_dict_for_key = {}
    for pattern, kind in _WEIGHT_SHARDING_TYPE.items():
      if not key.endswith(pattern):
        continue
      with torch.no_grad():
        if kind in ("ParallelEmbedding", "RowParallelLinear"):
          state_dict_for_key[key] = torch.cat(tensors, 1)
        elif kind == "ColumnParallelLinear":
          state_dict_for_key[key] = torch.cat(tensors, 0)
        else:
          if not all(
              torch.allclose(tensors[0], tensor, atol=1e-6)
              for tensor in tensors[1:]
          ):
            raise ValueError(
                f"Tensors must be identical across shards for {key}"
            )
          state_dict_for_key[key] = tensors[0]

        if enable_float32:
          state_dict_for_key[key] = state_dict_for_key[key].float()
    if minimize_memory_footprint:
      # Stage this merged weights on disk to reduce memory footprint.
      torch.save(state_dict_for_key, os.fspath(tmp_dir / (key + ".pth")))
      del state_dict_for_key
      gc.collect()
    else:
      state_dict.update(state_dict_for_key)

  if minimize_memory_footprint:
    # Release weights loaded into memory from the original checkpoint dir
    # before loading merged weights that were starged on disk.
    # Doing so could help with reducing memory usage.
    del checkpoints
    gc.collect()
    paths = tmp_dir.glob("*.pth")
    paths = sorted(paths)
    for path in paths:
      state_dict.update(
          torch.load(os.fspath(path), map_location=torch.device("cpu"))
      )
      # Delete the individual merged weight file to free up disk space
      # for merged single weight file below.
      epath.Path(path).unlink()
    tmp_dir.rmtree()
  return state_dict


def _load_from_gcs(input_ckpt_dir: epath.Path):
  checkpoints = []
  input_ckpt_dir_str = str(input_ckpt_dir)
  # pylint: disable-next=all
  bucket_name, blob_name = input_ckpt_dir_str.split("//")[-1].split("/", 1)
  print(f"Bucket {bucket_name}, blob {blob_name}")
  storage_client = storage.Client()
  input_blobs = storage_client.list_blobs(bucket_name, prefix=blob_name)
  for blob in input_blobs:
    if "params.json" in blob.name:
      with blob.open("r") as f:
        print(f"Loading parameter files from {blob.name}")
        params = f.read()
        f.close()
        print("params: ", params)
    if ".pth" in blob.name:
      print(f"Loading checkpoint files from {blob.name}")
      with blob.open("rb") as f:
        checkpoints += torch.load(f, map_location=torch.device("cpu"))
        f.close()
  return checkpoints, params


def _load_from_local(input_ckpt_dir: epath.Path):
  checkpoints = []
  params = json.loads((input_ckpt_dir / "params.json").read_text())

  print(f"Loading checkpoint files from {input_ckpt_dir}.")
  paths = input_ckpt_dir.glob("*.pth")
  paths = sorted(paths)
  checkpoints = [
      torch.load(os.fspath(path), map_location=torch.device("cpu"))
      for path in paths
  ]
  if not checkpoints:
    raise ValueError(f"No *.pth found in the input dir {input_ckpt_dir}")
  return checkpoints, params


def _export_to_gcs(output_ckpt_dir: epath.Path, params, state_dict):
  # pylint: disable-next=all
  bucket_name, output_ckpt = str(output_ckpt_dir).split("//")[-1].split("/", 1)
  print(f"Export to bucket {bucket_name}, blob {output_ckpt}")
  storage_client = storage.Client()
  bucket = storage_client.bucket(bucket_name)

  ckpt_blob = bucket.blob(os.path.join(output_ckpt, "consolidated.00.pth"))
  param_blob = bucket.blob(os.path.join(output_ckpt, "params.json"))
  checklist_blob = bucket.blob(os.path.join(output_ckpt, "checklist.chk"))
  with param_blob.open("w") as f:
    f.write(json.dumps(params))
    f.close()
  with ckpt_blob.open("w") as f:
    torch.save(state_dict, f)
    f.close()
  with checklist_blob.open("w") as f:
    f.write(_generate_md5_checklist(output_ckpt_dir))
    f.close()


def _export_to_local(output_ckpt_dir: epath.Path, params, state_dict):
  output_ckpt_dir.mkdir(parents=True, exist_ok=True)
  (output_ckpt_dir / "params.json").write_text(json.dumps(params))
  if _OUTPUT_SAFETENSORS.value:
    save_file(state_dict, os.fspath(output_ckpt_dir / "model.safetensors"))
  else:
    torch.save(state_dict, os.fspath(output_ckpt_dir / "consolidated.00.pth"))
    checklist_file = output_ckpt_dir / "checklist.chk"
    checklist_file.write_text(_generate_md5_checklist(output_ckpt_dir))


def _get_llama_state_dict(input_ckpt_dir):
  start = time.perf_counter()
  if "gs://" in str(input_ckpt_dir):
    print(
        """WARNING: Loading data from gcs bucket takes a lont time. 
        Suggest to download the data to local first!"""
    )
    checkpoints, params = _load_from_gcs(input_ckpt_dir)
  else:
    checkpoints, params = _load_from_local(input_ckpt_dir)
  end = time.perf_counter()
  print(f"Loading checkpoints takes {end - start} seconds")

  start = time.perf_counter()
  state_dict = _merge_llama_weights(
      checkpoints, _MINIMIZE_MEMORY_FOOTPRINT.value, _ENABLE_FLOAT32.value
  )
  end = time.perf_counter()
  print(f"Merging weights takes {end - start} seconds")
  return state_dict, params


def _get_gemma_state_dict(input_ckpt_dir):
  ckpt_file = list(input_ckpt_dir.glob("*.ckpt"))
  assert len(ckpt_file) == 1, "only expect 1 ckpt file for Gemma model."
  ckpt_file = ckpt_file[0]
  state_dict = torch.load(str(ckpt_file), map_location=torch.device("cpu"))[
      "model_state_dict"
  ]
  model_config = json.loads((input_ckpt_dir / "config.json").read_text())
  for key in list(state_dict.keys()):
    if state_dict[key].dtype.is_complex and _OUTPUT_SAFETENSORS.value:
      assert (
          key == "freqs_cis"
      ), "Only expect key 'freqs_cis' in the state_dict has complex dtype."
      # Remove "freqs_cis" since it has complex dtype, and safetensor doesn't support it.
      # The "freqs_cis" will be reconstructed when it's loaded by inference engine.
      state_dict.pop(key)
      continue
    prefix_to_remove = "model."
    new_key = key
    if key.startswith(prefix_to_remove):
      new_key = new_key.removeprefix(prefix_to_remove)
    if "qkv_proj" in key:
      q_dim = model_config["num_attention_heads"] * model_config["head_dim"]
      kv_dim = model_config["num_key_value_heads"] * model_config["head_dim"]
      qkv = state_dict.pop(key)
      q, k, v = qkv.split(
          [
              q_dim,
              kv_dim,
              kv_dim,
          ],
          dim=0,
      )
      state_dict[new_key.replace("qkv_proj", "wq")] = q
      state_dict[new_key.replace("qkv_proj", "wk")] = k
      state_dict[new_key.replace("qkv_proj", "wv")] = v
      continue

    if new_key != key:
      state_dict[new_key] = state_dict.pop(key)
  return state_dict, model_config


def main(argv) -> None:
  """merge weights"""

  if _MODEL_TYPE.value == "gemma":
    state_dict, params = _get_gemma_state_dict(_INPUT_CHECKPOINT_DIR.value)
    quantize_weight_map = _GEMMA_QUANTIZED_WEIGHTS_TO_SCALER_NAME
    weight_axis = lambda x: 0 if x == "embedder.weight" else 1
  else:
    state_dict, params = _get_llama_state_dict(_INPUT_CHECKPOINT_DIR.value)
    quantize_weight_map = _LLAMA_QUANTIZED_WEIGHTS_TO_SCALER_NAME
    weight_axis = lambda x: 0 if x == "tok_embeddings.weight" else 1

  if _QUANTIZE.value:
    start = time.perf_counter()
    state_dict = _quantize_state_dict(
        state_dict, quantize_weight_map, weight_axis
    )
    end = time.perf_counter()
    print(f"Quantizing weights takes {end - start} seconds")

  print(f"Writing merged weights to dir {_OUTPUT_CHECKPOINT_DIR.value}")
  start = time.perf_counter()
  if "gs://" in str(_OUTPUT_CHECKPOINT_DIR.value):
    _export_to_gcs(_OUTPUT_CHECKPOINT_DIR.value, params, state_dict)
  else:
    _export_to_local(_OUTPUT_CHECKPOINT_DIR.value, params, state_dict)
  end = time.perf_counter()
  print(f"Export outputs takes {end - start} seconds")


if __name__ == "__main__":
  app.run(main)
