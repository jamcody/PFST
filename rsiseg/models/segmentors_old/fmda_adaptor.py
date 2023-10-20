# Copyright (c) OpenMMLab. All rights reserved.
import torch
import torch.nn as nn
import torch.nn.functional as F
import pdb

from rsiseg.core import add_prefix
from rsiseg.ops import resize
from .. import builder
from ..builder import SEGMENTORS
from .base import BaseSegmentor
from mmcv.parallel import DataContainer
from mmcv.runner import BaseModule, auto_fp16


@SEGMENTORS.register_module()
class FMDAAdaptor(BaseSegmentor):
    """Encoder Decoder segmentors.

    EncoderDecoder typically consists of backbone, decode_head, auxiliary_head.
    Note that auxiliary_head is only used for deep supervision during training,
    which could be dumped during inference.
    """

    def __init__(self,
                 backbone,
                 decode_head,
                 neck=None,
                 auxiliary_head=None,
                 train_cfg=None,
                 test_cfg=None,
                 pretrained=None,
                 loss_sim_feat=None,
                 init_cfg=None):
        super(FMDAAdaptor, self).__init__(init_cfg)
        if pretrained is not None:
            assert backbone.get('pretrained') is None, \
                'both backbone and segmentor set pretrained weight'
            backbone.pretrained = pretrained
        self.backbone = builder.build_backbone(backbone)
        if neck is not None:
            self.neck = builder.build_neck(neck)
        self._init_decode_head(decode_head)
        self._init_auxiliary_head(auxiliary_head)
        if loss_sim_feat is not None:
            self.loss_sim_feat = builder.build_loss(loss_sim_feat)

        self.train_cfg = train_cfg
        self.test_cfg = test_cfg

        assert self.with_decode_head

    def _init_decode_head(self, decode_head):
        """Initialize ``decode_head``"""
        self.decode_head = builder.build_head(decode_head)
        self.align_corners = self.decode_head.align_corners
        self.num_classes = self.decode_head.num_classes

    def _init_auxiliary_head(self, auxiliary_head):
        """Initialize ``auxiliary_head``"""
        if auxiliary_head is not None:
            if isinstance(auxiliary_head, list):
                self.auxiliary_head = nn.ModuleList()
                for head_cfg in auxiliary_head:
                    self.auxiliary_head.append(builder.build_head(head_cfg))
            else:
                self.auxiliary_head = builder.build_head(auxiliary_head)

    def extract_feat(self, img):
        """Extract features from images."""
        x = self.backbone(img)
        if self.with_neck:
            x = self.neck(x)
        return x

    def encode_decode(self, img, img_metas):
        """Encode images with backbone and decode into a semantic segmentation
        map of the same size as input."""
        x = self.extract_feat(img)
        out = self._decode_head_forward_test(x, img_metas)
        out = resize(
            input=out,
            size=img.shape[2:],
            mode='bilinear',
            align_corners=self.align_corners)
        states = {'feats': x, 'seg_logits': out}
        return out, states

    def _decode_head_forward_train(self, x, img_metas, gt_semantic_seg, **kwargs):
        """Run forward function and calculate loss for decode head in
        training."""
        losses = dict()
        loss_decode, state = self.decode_head.forward_train(x, img_metas,
                                                            gt_semantic_seg,
                                                            self.train_cfg,
                                                            **kwargs)

        losses.update(add_prefix(loss_decode, 'decode'))
        return losses, state

    def _decode_head_forward_test(self, x, img_metas):
        """Run forward function and calculate loss for decode head in
        inference."""
        seg_logits = self.decode_head.forward_test(x, img_metas, self.test_cfg)
        return seg_logits

    def _auxiliary_head_forward_train(self, x, img_metas, gt_semantic_seg):
        """Run forward function and calculate loss for auxiliary head in
        training."""
        losses = dict()
        states = dict()
        if isinstance(self.auxiliary_head, nn.ModuleList):
            for idx, aux_head in enumerate(self.auxiliary_head):
                loss_aux, state_aux = aux_head.forward_train(x, img_metas,
                                                             gt_semantic_seg,
                                                             self.train_cfg)
                losses.update(add_prefix(loss_aux, f'aux_{idx}'))
                states.update(add_prefix(state_aux, f'aux_{idx}'))
        else:
            loss_aux, state_aux = self.auxiliary_head.forward_train(
                x, img_metas, gt_semantic_seg, self.train_cfg)
            losses.update(add_prefix(loss_aux, 'aux'))
            states.update(add_prefix(state_aux, 'aux'))

        return losses, states

    def forward_dummy(self, img):
        """Dummy forward function."""
        seg_logit, states = self.encode_decode(img, None)

        return seg_logit

    def forward_train(self, img, img_metas, gt_semantic_seg, return_states=False):
        """Forward function for training.

        Args:
            img (Tensor): Input images.
            img_metas (list[dict]): List of image info dict where each dict
                has: 'img_shape', 'scale_factor', 'flip', and may also contain
                'filename', 'ori_shape', 'pad_shape', and 'img_norm_cfg'.
                For details on the values of these keys see
                `rsiseg/datasets/pipelines/formatting.py:Collect`.
            gt_semantic_seg (Tensor): Semantic segmentation masks
                used if the architecture supports semantic segmentation task.

        Returns:
            dict[str, Tensor]: a dictionary of loss components
        """
        raise ValueError('Deprecated forward_train method! Use train_step instead')



    def train_step(self, data_batch, optimizer, **kwargs):
        """The iteration step during training.

        This method defines an iteration step during training, except for the
        back propagation and optimizer updating, which are done in an optimizer
        hook. Note that in some complicated cases or models, the whole process
        including back propagation and optimizer updating is also defined in
        this method, such as GAN.

        Args:
            data (dict): The output of dataloader.
            optimizer (:obj:`torch.optim.Optimizer` | dict): The optimizer of
                runner is passed to ``train_step()``. This argument is unused
                and reserved.

        Returns:
            dict: It should contain at least 3 keys: ``loss``, ``log_vars``,
                ``num_samples``.
                ``loss`` is a tensor for back propagation, which can be a
                weighted sum of multiple losses.
                ``log_vars`` contains all the variables to be sent to the
                logger.
                ``num_samples`` indicates the batch size (when the model is
                DDP, it means the batch size on each GPU), which is used for
                averaging the logs.
        """
        losses = dict()
        states = dict()

        img_metas_src, img_src, gt_src, img_metas_trg, img_trg, gt_trg, feats_trg = data_batch.values()



        feats_trg_list = []
        feats_trg = feats_trg.permute(0,3,1,2)
        for data, metas in zip(feats_trg, img_metas_trg):
            data = self.transform_by_metas(data.unsqueeze(0), metas)
            feats_trg_list.append(data.squeeze(0))
        feats_trg = torch.stack(feats_trg_list, dim=0)

        x_src = self.extract_feat(img_src)
        x_trg = self.extract_feat(img_trg)


        loss_dec_src, state_dec_src = self.decode_head.forward_train(x_src,
                                                                     img_metas_src,
                                                                     gt_src,
                                                                     self.train_cfg)

        loss_dec_trg, state_dec_trg = self.decode_head.forward_train(x_trg,
                                                                     img_metas_trg,
                                                                     gt_trg,
                                                                     self.train_cfg)

        loss_sim, state_sim_feat = self.loss_sim_feat(feats_trg, state_dec_trg['seg_logits'])

        state_dec_src['seg_logits'] = state_dec_src['seg_logits'].detach()
        state_dec_trg['seg_logits'] = state_dec_trg['seg_logits'].detach()

        losses.update(loss_sim)
        losses.update(add_prefix(loss_dec_src, 'src.dec'))
        losses.update(add_prefix(loss_dec_trg, 'trg.dec'))
        states.update(add_prefix(state_dec_src, 'src.dec'))
        states.update(add_prefix(state_dec_trg, 'trg.dec'))

        if self.with_auxiliary_head:
            loss_aux_src, state_aux_src = self._auxiliary_head_forward_train(
                x_src, img_metas_src, gt_src)

            loss_aux_trg, state_aux_trg = self._auxiliary_head_forward_train(
                x_trg, img_metas_trg, gt_trg)

            losses.update(add_prefix(loss_aux_src, 'src'))
            losses.update(add_prefix(loss_aux_trg, 'trg'))
            states.update(add_prefix(state_aux_src, 'src'))
            states.update(add_prefix(state_aux_trg, 'trg'))


        states.update({'vis|seg_mask_src': (img_src, gt_src,
                                            states['src.dec.seg_logits'].max(dim=1)[1].unsqueeze(1)),
                       'vis|seg_mask_trg': (img_trg, gt_trg,
                                            states['trg.dec.seg_logits'].max(dim=1)[1].unsqueeze(1)),
                       'vis|density_sim_feat': (img_trg, 1-state_sim_feat['sim_feat'].unsqueeze(1))})

        loss, log_vars = self._parse_losses(losses)

        outputs = dict(
            loss=loss,
            log_vars=log_vars,
            num_samples=len(img_metas_src) + len(img_metas_trg),
            states=states)

        return outputs

    # TODO refactor
    def slide_inference(self, img, img_meta, rescale):
        """Inference by sliding-window with overlap.

        If h_crop > h_img or w_crop > w_img, the small patch will be used to
        decode without padding.
        """

        h_stride, w_stride = self.test_cfg.stride
        h_crop, w_crop = self.test_cfg.crop_size
        batch_size, _, h_img, w_img = img.size()
        num_classes = self.num_classes
        h_grids = max(h_img - h_crop + h_stride - 1, 0) // h_stride + 1
        w_grids = max(w_img - w_crop + w_stride - 1, 0) // w_stride + 1
        preds = img.new_zeros((batch_size, num_classes, h_img, w_img))
        count_mat = img.new_zeros((batch_size, 1, h_img, w_img))
        for h_idx in range(h_grids):
            for w_idx in range(w_grids):
                y1 = h_idx * h_stride
                x1 = w_idx * w_stride
                y2 = min(y1 + h_crop, h_img)
                x2 = min(x1 + w_crop, w_img)
                y1 = max(y2 - h_crop, 0)
                x1 = max(x2 - w_crop, 0)
                crop_img = img[:, :, y1:y2, x1:x2]
                crop_seg_logit, _ = self.encode_decode(crop_img, img_meta)
                preds += F.pad(crop_seg_logit,
                               (int(x1), int(preds.shape[3] - x2), int(y1),
                                int(preds.shape[2] - y2)))

                count_mat[:, :, y1:y2, x1:x2] += 1
        assert (count_mat == 0).sum() == 0
        if torch.onnx.is_in_onnx_export():
            # cast count_mat to constant while exporting to ONNX
            count_mat = torch.from_numpy(
                count_mat.cpu().detach().numpy()).to(device=img.device)
        preds = preds / count_mat
        if rescale:
            preds = resize(
                preds,
                size=img_meta[0]['ori_shape'][:2],
                mode='bilinear',
                align_corners=self.align_corners,
                warning=False)
        return preds

    def whole_inference(self, img, img_meta, rescale):
        """Inference with full image."""

        seg_logit, states = self.encode_decode(img, img_meta)
        if rescale:
            # support dynamic shape for onnx
            if torch.onnx.is_in_onnx_export():
                size = img.shape[2:]
            else:
                size = img_meta[0]['ori_shape'][:2]
            seg_logit = resize(
                seg_logit,
                size=size,
                mode='bilinear',
                align_corners=self.align_corners,
                warning=False)

        return seg_logit, states

    def inference(self, img, img_meta, rescale):
        """Inference with slide/whole style.

        Args:
            img (Tensor): The input image of shape (N, 3, H, W).
            img_meta (dict): Image info dict where each dict has: 'img_shape',
                'scale_factor', 'flip', and may also contain
                'filename', 'ori_shape', 'pad_shape', and 'img_norm_cfg'.
                For details on the values of these keys see
                `rsiseg/datasets/pipelines/formatting.py:Collect`.
            rescale (bool): Whether rescale back to original shape.

        Returns:
            Tensor: The output segmentation map.
        """

        assert self.test_cfg.mode in ['slide', 'whole']
        if isinstance(img_meta, DataContainer):
            img_meta = img_meta.data
            img_meta = img_meta[0]

        ori_shape = img_meta[0]['ori_shape']
        assert all(_['ori_shape'] == ori_shape for _ in img_meta)
        if self.test_cfg.mode == 'slide':
            seg_logit = self.slide_inference(img, img_meta, rescale)
            states = {}
        else:
            seg_logit, states = self.whole_inference(img, img_meta, rescale)

        output = F.softmax(seg_logit, dim=1)
        flip = img_meta[0]['flip']
        if flip:
            pdb.set_trace()
            flip_direction = img_meta[0]['flip_direction']
            assert flip_direction in ['horizontal', 'vertical']
            if flip_direction == 'horizontal':
                output = output.flip(dims=(3, ))
            elif flip_direction == 'vertical':
                output = output.flip(dims=(2, ))

        return output, states

    def simple_test(self, img, img_meta, rescale=True):
        """Simple test with single image."""
        seg_logit, states = self.inference(img, img_meta, rescale)
        seg_pred = seg_logit.argmax(dim=1)
        if torch.onnx.is_in_onnx_export():
            # our inference backend only support 4D output
            seg_pred = seg_pred.unsqueeze(0)
            return seg_pred
        seg_pred = seg_pred.cpu().numpy()
        # unravel batch dim
        seg_pred = list(seg_pred)
        state_list = []

        for idx in range(len(seg_pred)):
            cur_state = {}
            cur_state['feats'] = [x[idx].cpu() for x in states['feats']]
            cur_state['seg_logits'] = states['seg_logits'][idx].cpu()
            state_list.append(cur_state)

        return seg_pred, state_list

    def aug_test(self, imgs, img_metas, rescale=True):
        """Test with augmentations.

        Only rescale=True is supported.
        """
        # aug_test rescale all imgs back to ori_shape for now
        assert rescale
        # to save memory, we get augmented seg logit inplace
        seg_logit = self.inference(imgs[0], img_metas[0], rescale)
        for i in range(1, len(imgs)):
            cur_seg_logit = self.inference(imgs[i], img_metas[i], rescale)
            seg_logit += cur_seg_logit
        seg_logit /= len(imgs)
        seg_pred = seg_logit.argmax(dim=1)
        seg_pred = seg_pred.cpu().numpy()
        # unravel batch dim
        seg_pred = list(seg_pred)
        return seg_pred

    def proportional_crop(self, data, crop_bbox, scale):
        """Crop from ``img``"""
        rescale = lambda x: int(x * scale)
        crop_y1, crop_y2, crop_x1, crop_x2 = map(rescale, crop_bbox)
        data = data[:, :, crop_y1:crop_y2, crop_x1:crop_x2]
        return data

    def transform_by_metas(self, data, metas):
        # data: (H, W, ...)

        if 'scale_factor' in metas:
            w_scale, h_scale, _, _ = metas['scale_factor']
            # H, W, C = metas['ori_shape']
            # new_h, new_w = int(H * h_scale), int(W * w_scale)
            # data = F.interpolate(data, size=(new_h, new_w), mode='nearest')
            data = F.interpolate(data, scale_factor=(w_scale, h_scale), mode='bilinear')

        if 'crop_bbox' in metas:
            w_scale, h_scale, _, _ = metas['scale_factor']
            assert w_scale == h_scale
            data = self.proportional_crop(data, metas['crop_bbox'], 1/8.)

            H, W, C = metas['ori_shape']
            new_h, new_w = int(H * h_scale), int(W * w_scale)

            data_h, data_w = data.shape[-2:]

        if 'rotate_k' in metas:
            data = torch.rot90(data, metas['rotate_k'], dims=[2,3])

        if 'flip_horizontal' in metas and metas['flip_horizontal']:
            data = data.flip(dims=[3])

        if 'flip_vertical' in metas and metas['flip_vertical']:
            data = data.flip(dims=[2])

        return data