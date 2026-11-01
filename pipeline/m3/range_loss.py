"""M3训练实验：仅把真值分配给DFL距离足够的锚点，推理和人工标注不变。"""
import torch
from ultralytics.utils.loss import v8DetectionLoss
from ultralytics.utils.tal import TaskAlignedAssigner, make_anchors
from ultralytics.utils.torch_utils import unwrap_model


class RangeAssigner(TaskAlignedAssigner):
    def select_candidates_in_gts(self, xy_centers, gt_bboxes, mask_gt, eps=1e-9):
        inside = super().select_candidates_in_gts(xy_centers, gt_bboxes, mask_gt, eps)
        strides = self.anchor_strides.to(xy_centers)
        distances = torch.cat((xy_centers[None, None] - gt_bboxes[:, :, None, :2],
                               gt_bboxes[:, :, None, 2:] - xy_centers[None, None]), -1)
        # 必须在top-k前排除；事后过滤可能删光正样本，且不会补选范围足够的层。
        representable = (distances / strides[None, None]).amax(-1) <= self.reg_max - 1 - .01
        eligible = inside.bool() & representable
        if not (eligible.any(-1) | ~mask_gt.squeeze(-1).bool()).all():
            raise ValueError('有真值框在所有预测层均无可表达锚点；请核查DFL范围和输入尺寸，不能静默丢弃真值')
        return eligible


class RangeLoss(v8DetectionLoss):
    def __init__(self, model):
        super().__init__(model)
        if self.reg_max <= 1 or getattr(model, 'end2end', False):
            raise ValueError('范围约束实验仅支持使用DFL的普通M3检测头')
        old = self.assigner
        self.assigner = RangeAssigner(topk=old.topk, num_classes=old.num_classes,
                                     alpha=old.alpha, beta=old.beta, stride=old.stride,
                                     eps=old.eps, topk2=old.topk2)
        self.assigner.reg_max = self.reg_max

    def get_assigned_targets_and_loss(self, preds, batch):
        if preds['boxes'].dtype == torch.bfloat16:
            # BF16范围更大，但坐标尾数太粗；分布积分、任务分配和几何损失仍用FP32。
            precise = dict(preds, boxes=preds['boxes'].float(), scores=preds['scores'].float())
            with torch.autocast(device_type=preds['boxes'].device.type, enabled=False):
                return self._range_loss(precise, batch)
        return self._range_loss(preds, batch)

    def _range_loss(self, preds, batch):
        self.assigner.anchor_strides = make_anchors(preds['feats'], self.stride, .5)[1]
        out = super().get_assigned_targets_and_loss(preds, batch)
        fg, _, targets, anchors, strides = out[0]
        if fg.any():
            distances = torch.cat((anchors[None] - targets[..., :2] / strides,
                                   targets[..., 2:] / strides - anchors[None]), -1)
            # 分配后再核对一次，包含多真值竞争同一锚点的情况；不修改损失或梯度。
            if distances[fg].max() > self.reg_max - 1 - .01 + 1e-3:
                raise ValueError('范围约束分配后仍出现超DFL距离的正样本')
        return out


def install_range_loss(trainer):
    model = unwrap_model(trainer.model)
    model.criterion = RangeLoss(model)
