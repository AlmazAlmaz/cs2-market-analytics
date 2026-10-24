"""Feature-token Transformer used by the Vega price model."""

from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional

ARCHITECTURE_ID = "feature_token_transformer_ordered_quantiles"


class SwiGLU(nn.Module):
    """Gated feed-forward layer with the Transformer input width restored."""

    def __init__(self, width: int, hidden: int, dropout: float) -> None:
        super().__init__()
        self.input = nn.Linear(width, hidden * 2)
        self.output = nn.Linear(hidden, width)
        self.dropout = nn.Dropout(dropout)

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        value, gate = self.input(values).chunk(2, dim=-1)
        return self.dropout(self.output(value * functional.silu(gate)))


class FeatureTransformerBlock(nn.Module):
    """Pre-normalized self-attention block backed by PyTorch SDPA."""

    def __init__(
        self,
        width: int,
        head_count: int,
        ffn_hidden: int,
        *,
        attention_dropout: float,
        residual_dropout: float,
    ) -> None:
        super().__init__()
        if width % head_count:
            raise ValueError("Transformer width must be divisible by head_count")
        self.head_count = int(head_count)
        self.head_width = int(width // head_count)
        self.attention_dropout = float(attention_dropout)
        self.attention_norm = nn.LayerNorm(width)
        self.qkv = nn.Linear(width, width * 3)
        self.attention_output = nn.Linear(width, width)
        self.attention_residual_dropout = nn.Dropout(residual_dropout)
        self.ffn_norm = nn.LayerNorm(width)
        self.ffn = SwiGLU(width, ffn_hidden, residual_dropout)

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        normalized = self.attention_norm(values)
        batch, tokens, width = normalized.shape
        qkv = self.qkv(normalized).reshape(
            batch,
            tokens,
            3,
            self.head_count,
            self.head_width,
        )
        query, key, value = qkv.unbind(dim=2)
        query = query.transpose(1, 2)
        key = key.transpose(1, 2)
        value = value.transpose(1, 2)
        attended = functional.scaled_dot_product_attention(
            query,
            key,
            value,
            dropout_p=self.attention_dropout if self.training else 0.0,
        )
        attended = attended.transpose(1, 2).reshape(batch, tokens, width)
        values = values + self.attention_residual_dropout(
            self.attention_output(attended)
        )
        return values + self.ffn(self.ffn_norm(values))


class VegaPriceTransformer(nn.Module):
    """69-token Transformer with intrinsically ordered q15/q50/q85 output."""

    def __init__(
        self,
        category_cardinalities: list[int],
        numeric_count: int,
        *,
        d_token: int = 192,
        block_count: int = 4,
        head_count: int = 8,
        ffn_hidden: int = 768,
        attention_dropout: float = 0.05,
        residual_dropout: float = 0.08,
        head_dropout: float = 0.05,
    ) -> None:
        super().__init__()
        if not category_cardinalities:
            raise ValueError("Vega needs at least one categorical feature")
        if any(int(value) < 2 for value in category_cardinalities):
            raise ValueError("Every Vega categorical cardinality must be at least 2")
        if int(numeric_count) < 1:
            raise ValueError("Vega needs at least one numeric feature")
        if int(d_token) < 8 or int(d_token) % int(head_count):
            raise ValueError("d_token must be at least 8 and divisible by head_count")
        if int(block_count) < 1:
            raise ValueError("Vega needs at least one Transformer block")
        if int(ffn_hidden) < int(d_token):
            raise ValueError("ffn_hidden must not be narrower than d_token")
        for name, value in (
            ("attention_dropout", attention_dropout),
            ("residual_dropout", residual_dropout),
            ("head_dropout", head_dropout),
        ):
            if not 0.0 <= float(value) < 1.0:
                raise ValueError(f"{name} must be in [0, 1)")

        self.category_count = len(category_cardinalities)
        self.numeric_count = int(numeric_count)
        self.d_token = int(d_token)
        self.token_count = 1 + self.category_count + self.numeric_count
        self.category_embeddings = nn.ModuleList(
            nn.Embedding(int(cardinality), self.d_token, padding_idx=0)
            for cardinality in category_cardinalities
        )
        self.numeric_weight = nn.Parameter(
            torch.empty(self.numeric_count, self.d_token)
        )
        self.numeric_bias = nn.Parameter(torch.empty(self.numeric_count, self.d_token))
        self.field_embedding = nn.Parameter(
            torch.empty(self.category_count + self.numeric_count, self.d_token)
        )
        self.cls_token = nn.Parameter(torch.empty(1, 1, self.d_token))
        self.blocks = nn.Sequential(
            *(
                FeatureTransformerBlock(
                    self.d_token,
                    int(head_count),
                    int(ffn_hidden),
                    attention_dropout=float(attention_dropout),
                    residual_dropout=float(residual_dropout),
                )
                for _ in range(int(block_count))
            )
        )
        self.output_norm = nn.LayerNorm(self.d_token)
        self.head = nn.Sequential(
            nn.Linear(self.d_token, self.d_token * 2),
            nn.SiLU(),
            nn.Dropout(float(head_dropout)),
            nn.Linear(self.d_token * 2, 3),
        )
        self.reset_parameters()

    def reset_parameters(self) -> None:
        nn.init.normal_(self.numeric_weight, mean=0.0, std=0.02)
        nn.init.normal_(self.numeric_bias, mean=0.0, std=0.02)
        nn.init.normal_(self.field_embedding, mean=0.0, std=0.02)
        nn.init.normal_(self.cls_token, mean=0.0, std=0.02)
        for embedding in self.category_embeddings:
            nn.init.normal_(embedding.weight, mean=0.0, std=0.025)
            with torch.no_grad():
                embedding.weight[0].zero_()

    def tokenize(
        self, categories: torch.Tensor, numeric: torch.Tensor
    ) -> torch.Tensor:
        if categories.ndim != 2 or categories.shape[1] != self.category_count:
            raise ValueError("Vega categorical tensor has the wrong shape")
        if numeric.ndim != 2 or numeric.shape[1] != self.numeric_count:
            raise ValueError("Vega numeric tensor has the wrong shape")
        if categories.shape[0] != numeric.shape[0]:
            raise ValueError("Vega categorical/numeric batch sizes differ")

        categorical_tokens = torch.stack(
            [
                embedding(categories[:, index])
                for index, embedding in enumerate(self.category_embeddings)
            ],
            dim=1,
        )
        numeric_tokens = (
            numeric.unsqueeze(-1) * self.numeric_weight.unsqueeze(0)
            + self.numeric_bias.unsqueeze(0)
        )
        feature_tokens = torch.cat((categorical_tokens, numeric_tokens), dim=1)
        feature_tokens = feature_tokens + self.field_embedding.unsqueeze(0)
        cls = self.cls_token.expand(categories.shape[0], -1, -1)
        return torch.cat((cls, feature_tokens), dim=1)

    def forward(
        self, categories: torch.Tensor, numeric: torch.Tensor
    ) -> torch.Tensor:
        values = self.blocks(self.tokenize(categories, numeric))
        raw = self.head(self.output_norm(values[:, 0]))
        center = raw[:, 0]
        low = center - functional.softplus(raw[:, 1])
        high = center + functional.softplus(raw[:, 2])
        return torch.stack((low, center, high), dim=1)


# Stable artifact/runtime name.  Keeping the descriptive class above makes the
# architecture obvious while validators can depend on one compact public API.
VegaPriceNet = VegaPriceTransformer
