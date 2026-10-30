from dataclasses import asdict, dataclass

import torch
from torch import nn
from torch.nn import functional as F


@dataclass
class ModelConfig:
    vocab_size: int
    context_length: int = 512
    d_model: int = 256
    n_layers: int = 4
    n_heads: int = 8
    d_ff: int = 1024
    dropout: float = 0.1
    pad_id: int = 0


class Block(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.n_heads = config.n_heads
        self.dropout = config.dropout
        self.norm1 = nn.LayerNorm(config.d_model)
        self.qkv = nn.Linear(config.d_model, 3 * config.d_model)
        self.proj = nn.Linear(config.d_model, config.d_model)
        self.norm2 = nn.LayerNorm(config.d_model)
        self.ff = nn.Sequential(nn.Linear(config.d_model, config.d_ff), nn.GELU(),
                                nn.Linear(config.d_ff, config.d_model))
        self.residual_dropout = nn.Dropout(config.dropout)

    def forward(self, x):
        batch, length, width = x.shape
        q, k, v = self.qkv(self.norm1(x)).chunk(3, dim=-1)
        q, k, v = [t.view(batch, length, self.n_heads, width // self.n_heads).transpose(1, 2) for t in (q, k, v)]
        # Right padding is exclusively in the future of valid tokens. Causality
        # therefore excludes padding for all logits whose targets enter the loss.
        attn = F.scaled_dot_product_attention(q, k, v, is_causal=True,
                    dropout_p=self.dropout if self.training else 0.0)
        attn = attn.transpose(1, 2).contiguous().view(batch, length, width)
        x = x + self.residual_dropout(self.proj(attn))
        return x + self.residual_dropout(self.ff(self.norm2(x)))


class RhythmTransformer(nn.Module):
    def __init__(self, config):
        super().__init__()
        if config.d_model % config.n_heads:
            raise ValueError("d_model must be divisible by n_heads")
        self.config = config
        self.embedding = nn.Embedding(config.vocab_size, config.d_model, padding_idx=config.pad_id)
        self.position = nn.Embedding(config.context_length, config.d_model)
        self.dropout = nn.Dropout(config.dropout)
        self.blocks = nn.ModuleList([Block(config) for _ in range(config.n_layers)])
        self.norm = nn.LayerNorm(config.d_model)
        self.head = nn.Linear(config.d_model, config.vocab_size)
        self.apply(self._init)
        with torch.no_grad():
            self.embedding.weight[config.pad_id].zero_()

    @staticmethod
    def _init(module):
        if isinstance(module, (nn.Linear, nn.Embedding)):
            nn.init.normal_(module.weight, mean=0.0, std=0.02)
            if getattr(module, "bias", None) is not None:
                nn.init.zeros_(module.bias)

    def forward(self, ids):
        if ids.ndim != 2 or ids.shape[1] > self.config.context_length:
            raise ValueError("Expected [batch, sequence] within context_length")
        positions = torch.arange(ids.shape[1], device=ids.device)
        x = self.dropout(self.embedding(ids) + self.position(positions)[None, :, :])
        for block in self.blocks:
            x = block(x)
        return self.head(self.norm(x))

    def expanded(self, new_vocab_size):
        if new_vocab_size < self.config.vocab_size:
            raise ValueError("Vocabulary expansion cannot remove existing token IDs")
        cfg = asdict(self.config)
        cfg["vocab_size"] = new_vocab_size
        expanded = RhythmTransformer(ModelConfig(**cfg))
        old_state = self.state_dict()
        new_state = expanded.state_dict()
        for key, value in old_state.items():
            if key in {"embedding.weight", "head.weight", "head.bias"}:
                new_state[key][:self.config.vocab_size].copy_(value)
            else:
                new_state[key].copy_(value)
        expanded.load_state_dict(new_state)
        return expanded
