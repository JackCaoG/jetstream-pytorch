import jax
import torch
import jax
from jetstream_pt.third_party.llama import model_args
from jetstream_pt import environment


def make_env_tiny(bf16_enable=True):
  torch_dtype = torch.bfloat16 if bf16_enable else torch.float32
  torch.set_default_dtype(torch_dtype)
  jax.config.update("jax_dynamic_shapes", False)
  jax.config.update("jax_traceback_filtering", "off")
  config = model_args.get_model_args("llama-2-tiny", 128, 1, True)
  environment_data = environment.JetEngineEnvironmentData()
  environment_data.max_input_sequence_length = 128
  environment_data.max_input_sequence_length = 128
  environment_data.cache_sequence_length = 128
  environment_data.bf16_enable = bf16_enable
  environment_data.model_type = "llama-2-tiny"
  environment_data.batch_size = 1
  environment_data.num_layers = config.n_layers
  environment_data.cache_shape = (
      1,
      config.n_kv_heads,
      environment_data.cache_sequence_length,
      config.dim // config.n_heads,
  )
  env = environment.JetEngineEnvironment(environment_data)
  env.apply_sharding = lambda *args, **kwargs: None  # don't shard on cpu
  return env, config
