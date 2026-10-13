"""Fully offline, tiny official MLX-LM fixture; no handoff-specific assets needed."""

from pathlib import Path
import subprocess


def create_model(python, destination, *, shards=False):
    code = r"""
import json,sys
from pathlib import Path
from functools import partial
import mlx.core as mx
from mlx_lm.models.llama import Model,ModelArgs
from mlx_lm import utils
from tokenizers import Tokenizer,models,pre_tokenizers
from transformers import PreTrainedTokenizerFast
root=Path(sys.argv[1]);root.mkdir()
config=dict(model_type='llama',hidden_size=32,num_hidden_layers=1,intermediate_size=64,num_attention_heads=4,num_key_value_heads=4,rms_norm_eps=1e-5,vocab_size=16,max_position_embeddings=128,tie_word_embeddings=False)
mx.set_default_device(mx.gpu);mx.random.seed(5)
model=Model(ModelArgs(**config));mx.eval(model.parameters())
if sys.argv[2]=='shards':utils.make_shards=partial(utils.make_shards,max_file_size_gb=0)
utils.save_model(root,model,donate_model=False)
(root/'config.json').write_text(json.dumps(config,sort_keys=True))
vocab={'<pad>':0,'<unk>':1,'<bos>':2,'<eos>':3,**{f't{i}':i for i in range(4,16)}}
t=Tokenizer(models.WordLevel(vocab,unk_token='<unk>'));t.pre_tokenizer=pre_tokenizers.Whitespace()
PreTrainedTokenizerFast(tokenizer_object=t,unk_token='<unk>',bos_token='<bos>',eos_token='<eos>',pad_token='<pad>').save_pretrained(root)
"""
    subprocess.run(
        [str(python), "-I", "-c", code, str(destination), "shards" if shards else "normal"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return Path(destination)
