"""Compact residual embedding network for tabular CS2 pricing data."""

from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as functional


class ResidualBlock(nn.Module):
    def __init__(self, width: int, dropout: float) -> None:
        super().__init__()
        self.norm = nn.LayerNorm(width)
        self.layers = nn.Sequential(
            nn.Linear(width, width * 2),
            nn.SiLU(),
            nn.Dropout(dropout),
            nn.Linear(width * 2, width),
            nn.Dropout(dropout),
        )

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        return values + self.layers(self.norm(values))


class NovaPriceNet(nn.Module):
    """Embedding MLP with intrinsically ordered 15/50/85% quantiles."""

    def __init__(
        self,
        category_cardinalities: list[int],
        embedding_dims: list[int],
        numeric_count: int,
        *,
        width: int = 384,
        block_count: int = 4,
        dropout: float = 0.10,
    ) -> None:
        super().__init__()
        if len(category_cardinalities) != len(embedding_dims):
            raise ValueError("Each categorical feature needs an embedding dimension")

        self.embeddings = nn.ModuleList(
            nn.Embedding(cardinality, dimension, padding_idx=0)
            for cardinality, dimension in zip(category_cardinalities, embedding_dims, strict=True)
        )
        input_width = sum(embedding_dims) + numeric_count
        self.stem = nn.Sequential(
            nn.Linear(input_width, width),
            nn.LayerNorm(width),
            nn.SiLU(),
            nn.Dropout(dropout),
        )
        self.blocks = nn.Sequential(*(ResidualBlock(width, dropout) for _ in range(block_count)))
        self.head = nn.Sequential(
            nn.LayerNorm(width),
            nn.Linear(width, width // 2),
            nn.SiLU(),
            nn.Linear(width // 2, 3),
        )

        self.reset_parameters()

    def reset_parameters(self) -> None:
        for embedding in self.embeddings:
            nn.init.normal_(embedding.weight, mean=0.0, std=0.025)
            with torch.no_grad():
                embedding.weight[0].zero_()

    def forward(self, categories: torch.Tensor, numeric: torch.Tensor) -> torch.Tensor:
        embedded = [layer(categories[:, index]) for index, layer in enumerate(self.embeddings)]
        values = torch.cat([*embedded, numeric], dim=1)
        raw = self.head(self.blocks(self.stem(values)))
        center = raw[:, 0]
        low = center - functional.softplus(raw[:, 1])
        high = center + functional.softplus(raw[:, 2])
        return torch.stack((low, center, high), dim=1)
