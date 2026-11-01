"""整行涂黑：保留横向特征，双向序列建模，再由A–F各自查询相关位置。"""
import math

import torch
from torch import nn


class SequenceNet(nn.Module):
    def __init__(self):
        super().__init__()
        layers = []
        channels = 1
        for out, pool in ((16, (2, 2)), (32, (2, 2)),
                          (64, (2, 1)), (64, (2, 1))):
            layers += [nn.Conv2d(channels, out, 3, padding=1),
                       nn.BatchNorm2d(out), nn.ReLU(), nn.MaxPool2d(pool)]
            channels = out
        self.features = nn.Sequential(*layers)
        self.sequence = nn.GRU(64, 64, batch_first=True, bidirectional=True)
        self.position = nn.Linear(1, 128)
        self.queries = nn.Parameter(torch.randn(6, 128) * 0.02)
        self.head = nn.Sequential(nn.Linear(128, 64), nn.ReLU(),
                                  nn.Dropout(0.3), nn.Linear(64, 1))

    def forward(self, x):
        # 横向仅缩小4倍；旧整行分类网络缩小16倍。
        seq = self.features(x).mean(dim=2).transpose(1, 2)
        seq, _ = self.sequence(seq)
        pos = torch.linspace(-1, 1, seq.size(1), device=x.device,
                             dtype=x.dtype).view(1, -1, 1)
        keys = seq + self.position(pos)
        scores = torch.einsum('btd,ld->blt', keys, self.queries) / math.sqrt(128)
        context = torch.bmm(scores.softmax(dim=-1), seq)
        return self.head(context + self.queries.unsqueeze(0)).squeeze(-1)
