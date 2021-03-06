#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: imagenet_utils.py


import cv2
import numpy as np
import multiprocessing
import tensorflow as tf
from abc import abstractmethod

from tensorpack import imgaug, dataset, ModelDesc
from tensorpack.dataflow import (
    BatchData, MultiThreadMapData, DataFromList)
from tensorpack.predict import PredictConfig, SimpleDatasetPredictor
from tensorpack.utils.stats import RatioCounter
from tensorpack.models import regularize_cost
from tensorpack.tfutils.summary import add_moving_summary
from tensorpack.utils import logger


class GoogleNetResize(imgaug.ImageAugmentor):
    """
    crop 8%~100% of the original image
    See `Going Deeper with Convolutions` by Google.
    """
    def __init__(self, crop_area_fraction=0.08,
                 aspect_ratio_low=0.75, aspect_ratio_high=1.333,
                 target_shape=224):
        self._init(locals())

    def _augment(self, img, _):
        h, w = img.shape[:2]
        area = h * w
        for _ in range(10):
            targetArea = self.rng.uniform(self.crop_area_fraction, 1.0) * area
            aspectR = self.rng.uniform(self.aspect_ratio_low, self.aspect_ratio_high)
            ww = int(np.sqrt(targetArea * aspectR) + 0.5)
            hh = int(np.sqrt(targetArea / aspectR) + 0.5)
            if self.rng.uniform() < 0.5:
                ww, hh = hh, ww
            if hh <= h and ww <= w:
                x1 = 0 if w == ww else self.rng.randint(0, w - ww)
                y1 = 0 if h == hh else self.rng.randint(0, h - hh)
                out = img[y1:y1 + hh, x1:x1 + ww]
                out = cv2.resize(out, (self.target_shape, self.target_shape), interpolation=cv2.INTER_CUBIC)
                return out
        out = imgaug.ResizeShortestEdge(self.target_shape, interp=cv2.INTER_CUBIC).augment(img)
        out = imgaug.CenterCrop(self.target_shape).augment(out)
        return out


def small_augmentor():
    augmentors = [
        GoogleNetResize(),
        imgaug.Lighting(0.1,
                        eigval=np.asarray(
                            [0.2175, 0.0188, 0.0045][::-1]) * 255.0,
                        eigvec=np.array(
                            [[-0.5675, 0.7192, 0.4009],
                             [-0.5808, -0.0045, -0.8140],
                             [-0.5836, -0.6948, 0.4203]],
                            dtype='float32')[::-1, ::-1]),
        imgaug.Flip(horiz=True),
    ]
    return augmentors


def fbresnet_augmentor(isTrain):
    """
    Augmentor used in fb.resnet.torch, for BGR images in range [0,255].
    """
    if isTrain:
        """
        Sec 5.1:
        We use scale and aspect ratio data augmentation [35] as
        in [12]. The network input image is a 224×224 pixel random
        crop from an augmented image or its horizontal flip.
        """
        augmentors = [
            GoogleNetResize(),
            imgaug.RandomOrderAug(
                [imgaug.BrightnessScale((0.6, 1.4), clip=False),
                 imgaug.Contrast((0.6, 1.4), clip=False),
                 imgaug.Saturation(0.4, rgb=False),
                 # rgb-bgr conversion for the constants copied from fb.resnet.torch
                 imgaug.Lighting(0.1,
                                 eigval=np.asarray(
                                     [0.2175, 0.0188, 0.0045][::-1]) * 255.0,
                                 eigvec=np.array(
                                     [[-0.5675, 0.7192, 0.4009],
                                      [-0.5808, -0.0045, -0.8140],
                                      [-0.5836, -0.6948, 0.4203]],
                                     dtype='float32')[::-1, ::-1]
                                 )]),
            imgaug.Flip(horiz=True),
        ]
    else:
        augmentors = [
            imgaug.ResizeShortestEdge(256, cv2.INTER_CUBIC),
            imgaug.CenterCrop((224, 224)),
        ]
    return augmentors


def get_val_dataflow(
        datadir, batch_size,
        augmentors, parallel=None,
        num_splits=None, split_index=None):
    assert datadir is not None
    assert isinstance(augmentors, list)
    if parallel is None:
        parallel = min(40, multiprocessing.cpu_count())

    if num_splits is None:
        ds = dataset.ILSVRC12Files(datadir, 'val', shuffle=False)
    else:
        # shard validation data
        assert split_index < num_splits
        files = dataset.ILSVRC12Files(datadir, 'val', shuffle=False)
        files.reset_state()
        files = list(files.get_data())
        logger.info("Number of validation data = {}".format(len(files)))
        split_size = len(files) // num_splits
        start, end = split_size * split_index, split_size * (split_index + 1)
        end = min(end, len(files))
        logger.info("Local validation split = {} - {}".format(start, end))
        files = files[start: end]
        ds = DataFromList(files, shuffle=False)
    aug = imgaug.AugmentorList(augmentors)

    def mapf(dp):
        fname, cls = dp
        im = cv2.imread(fname, cv2.IMREAD_COLOR)
        im = aug.augment(im)
        return im, cls
    ds = MultiThreadMapData(ds, parallel, mapf,
                            buffer_size=min(2000, ds.size()), strict=True)
    ds = BatchData(ds, batch_size, remainder=True)
    # do not fork() under MPI
    return ds


def eval_on_ILSVRC12(model, sessinit, dataflow):
    pred_config = PredictConfig(
        model=model,
        session_init=sessinit,
        input_names=['input', 'label'],
        output_names=['wrong-top1', 'wrong-top5']
    )
    pred = SimpleDatasetPredictor(pred_config, dataflow)
    acc1, acc5 = RatioCounter(), RatioCounter()
    for top1, top5 in pred.get_result():
        batch_size = top1.shape[0]
        acc1.feed(top1.sum(), batch_size)
        acc5.feed(top5.sum(), batch_size)
    print("Top1 Error: {}".format(acc1.ratio))
    print("Top5 Error: {}".format(acc5.ratio))


class ImageNetModel(ModelDesc):
    weight_decay = 1e-4
    image_shape = 224
    image_dtype = tf.uint8

    def inputs(self):
        return [tf.placeholder(self.image_dtype, [None, self.image_shape, self.image_shape, 3], 'input'),
                tf.placeholder(tf.int32, [None], 'label')]

    def build_graph(self, image, label):
        image = ImageNetModel.image_preprocess(image, bgr=True)
        image = tf.transpose(image, [0, 3, 1, 2])

        logits = self.get_logits(image)
        loss = ImageNetModel.compute_loss_and_error(logits, label)

        if self.weight_decay > 0:
            """
            Sec 5.1: We use a weight decay λ of 0.0001 and following [16] we do not apply
            weight decay on the learnable BN coefficients
            """
            wd_loss = regularize_cost('.*/W', tf.contrib.layers.l2_regularizer(self.weight_decay),
                                      name='l2_regularize_loss')
            add_moving_summary(loss, wd_loss)
            ret = tf.add_n([loss, wd_loss], name='cost')
        else:
            ret = tf.identity(loss, name='cost')
            add_moving_summary(ret)
        return ret

    @abstractmethod
    def get_logits(self, image):
        """
        Args:
            image: 4D tensor of 224x224 in ``self.data_format``

        Returns:
            Nx1000 logits
        """

    def optimizer(self):
        """
        Sec 5.1: We use Nesterov momentum with m of 0.9.

        Sec 3: momentum correction
        Tensorflow's momentum optimizer does not need correction.
        """
        lr = tf.get_variable('learning_rate', initializer=0.1, trainable=False)
        tf.summary.scalar('learning_rate-summary', lr)
        return tf.train.MomentumOptimizer(lr, 0.9, use_nesterov=True)

    @staticmethod
    def image_preprocess(image, bgr=True):
        with tf.name_scope('image_preprocess'):
            if image.dtype.base_dtype != tf.float32:
                image = tf.cast(image, tf.float32)
            image = image * (1.0 / 255)

            """
            Sec 5.1:
            The input image is normalized by the per-color mean and
            standard deviation, as in [12]
            """
            mean = [0.485, 0.456, 0.406]    # rgb
            std = [0.229, 0.224, 0.225]
            if bgr:
                mean = mean[::-1]
                std = std[::-1]
            image_mean = tf.constant(mean, dtype=tf.float32)
            image_std = tf.constant(std, dtype=tf.float32)
            image = (image - image_mean) / image_std
            return image

    @staticmethod
    def compute_loss_and_error(logits, label):
        loss = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=label)
        loss = tf.reduce_mean(loss, name='xentropy-loss')

        def prediction_incorrect(logits, label, topk=1, name='incorrect_vector'):
            with tf.name_scope('prediction_incorrect'):
                x = tf.logical_not(tf.nn.in_top_k(logits, label, topk))
            return tf.cast(x, tf.float32, name=name)

        wrong = prediction_incorrect(logits, label, 1, name='wrong-top1')
        add_moving_summary(tf.reduce_mean(wrong, name='train-error-top1'))

        wrong = prediction_incorrect(logits, label, 5, name='wrong-top5')
        add_moving_summary(tf.reduce_mean(wrong, name='train-error-top5'))
        return loss
